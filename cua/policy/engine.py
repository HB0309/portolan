"""The policy gate.

Every action, in both discovery and replay, passes through ``PolicyEngine.check``
before it reaches a surface. The gate lives here -- in the runtime, between the
executor and the browser -- rather than in the agent's system prompt, because a
guardrail a model can talk itself out of is not a guardrail. The discovery agent
is told about the allowlist so it can plan sensibly, but being told is not what
enforces it.

Two classes of decision:

* **Allowlist.** Origin, path and action type. Anything outside is denied
  outright, and a denial is a first-class outcome the caller sees -- never a
  silently skipped step.

* **Reversibility.** Actions that activate a control whose accessible name looks
  like a commitment ("Transfer", "Submit", "Open Sub-Account") are classified
  IRREVERSIBLE. During discovery those always stop for a human: a model that is
  still working out how the app behaves has no standing to move someone's money.
  During replay they are permitted only when a reviewer has approved both the
  step and the capability.

The name-pattern classifier is a heuristic and is described as one in the report.
It is deliberately over-inclusive: a false positive costs one human confirmation,
a false negative costs an irreversible transaction on a real member's account.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from cua.policy.redaction import Redactor
from cua.schema.capability import ElementDescriptor, RiskClass

DEFAULT_POLICY_PATH = Path(__file__).with_name("policy.yaml")


class Mode(str, Enum):
    """Which execution path is asking. The answer to a risky action differs."""

    DISCOVERY = "discovery"
    REPLAY = "replay"


class Disposition(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class PolicyVerdict:
    disposition: Disposition
    risk: RiskClass
    reason: str = ""
    rule: str = ""

    @property
    def allowed(self) -> bool:
        return self.disposition is Disposition.ALLOW


@dataclass
class Limits:
    max_steps: int = 25
    wall_clock_seconds: int = 300
    max_consecutive_noops: int = 3


class PolicyEngine:
    def __init__(self, config: dict[str, Any]) -> None:
        allowlist = config.get("allowlist", {})
        self._origins: list[str] = [o.rstrip("/") for o in allowlist.get("origins", [])]
        self._path_patterns: list[str] = allowlist.get("path_patterns", ["*"])
        self._actions: set[str] = set(allowlist.get("actions", []))

        risk = config.get("risk", {})
        self._irreversible_patterns: list[str] = [
            p.lower() for p in risk.get("irreversible_name_patterns", [])
        ]
        self._discovery_disposition: str = risk.get("discovery", "escalate")
        self._replay_disposition: str = risk.get("replay", "require_approval")
        self.allow_bbox_fallback: bool = bool(risk.get("allow_bbox_fallback", False))

        limits = config.get("limits", {})
        self.limits = Limits(
            max_steps=limits.get("max_steps", 25),
            wall_clock_seconds=limits.get("wall_clock_seconds", 300),
            max_consecutive_noops=limits.get("max_consecutive_noops", 3),
        )

        self.redactor = Redactor.from_config(config.get("redaction", {}))

    @classmethod
    def load(cls, path: str | Path | None = None) -> PolicyEngine:
        target = Path(path) if path else DEFAULT_POLICY_PATH
        with open(target, "r", encoding="utf-8") as handle:
            return cls(yaml.safe_load(handle) or {})

    # -- allowlist -------------------------------------------------------

    def url_allowed(self, url: str) -> tuple[bool, str]:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False, f"{url!r} is not an absolute URL"

        origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        if origin not in self._origins:
            return False, f"origin {origin!r} is not on the allowlist"

        path = parsed.path or "/"
        if not any(fnmatch.fnmatch(path, pattern) for pattern in self._path_patterns):
            return False, f"path {path!r} is not on the allowlist for {origin!r}"

        return True, ""

    def action_type_allowed(self, action_type: str) -> bool:
        return action_type in self._actions

    # -- reversibility ---------------------------------------------------

    #: Roles that can commit a change. A link with a committing-sounding name is
    #: almost always navigation to the form rather than the act of submitting it
    #: -- "Open Sub-Account" names both the link that opens the form and the
    #: button that commits it, and treating the link as irreversible stops the
    #: agent before it can even look at the screen it needs.
    _COMMITTING_ROLES = {"button", "menuitem"}

    def classify_risk(
        self, action_type: str, target: ElementDescriptor | None
    ) -> tuple[RiskClass, str]:
        """Decide whether an action commits something that cannot be undone.

        Two signals, both required: the control must be one that can commit, and
        its name must read like a commitment. Requiring both is what keeps this
        from firing on every link whose caption happens to contain a verb, while
        still catching the thing that actually moves money.

        It remains a heuristic, and it is deliberately biased: a false positive
        costs one human confirmation, a false negative costs an irreversible
        transaction on a member's account.
        """
        if action_type not in {"click", "press_key"}:
            return RiskClass.SAFE, ""
        if target is not None and target.role not in self._COMMITTING_ROLES:
            return RiskClass.SAFE, ""

        name = (target.accessible_name if target else None) or ""
        lowered = name.lower()
        for pattern in self._irreversible_patterns:
            if pattern in lowered:
                return RiskClass.IRREVERSIBLE, (
                    f"{target.role if target else 'control'} named {name!r} matches "
                    f"the irreversible pattern {pattern!r}"
                )
        return RiskClass.SAFE, ""

    # -- the gate --------------------------------------------------------

    def check(
        self,
        *,
        mode: Mode,
        action_type: str,
        url: str | None = None,
        target: ElementDescriptor | None = None,
        step_approved_risky: bool = False,
        capability_approved: bool = False,
    ) -> PolicyVerdict:
        """The single decision point. Nothing reaches a surface without it."""
        if not self.action_type_allowed(action_type):
            return PolicyVerdict(
                Disposition.DENY,
                RiskClass.SAFE,
                reason=f"action type {action_type!r} is not permitted",
                rule="allowlist.actions",
            )

        if url is not None:
            ok, why = self.url_allowed(url)
            if not ok:
                return PolicyVerdict(
                    Disposition.DENY, RiskClass.SAFE, reason=why, rule="allowlist.origins"
                )

        risk, why = self.classify_risk(action_type, target)
        if risk is RiskClass.SAFE:
            return PolicyVerdict(Disposition.ALLOW, RiskClass.SAFE)

        if mode is Mode.DISCOVERY:
            if self._discovery_disposition == "allow":
                return PolicyVerdict(Disposition.ALLOW, risk, reason=why, rule="risk.discovery")
            disposition = (
                Disposition.DENY
                if self._discovery_disposition == "block"
                else Disposition.ESCALATE
            )
            return PolicyVerdict(
                disposition,
                risk,
                reason=(
                    f"irreversible action during discovery ({why}); a human decides "
                    "whether to commit it"
                ),
                rule="risk.discovery",
            )

        # Replay.
        if self._replay_disposition == "allow":
            return PolicyVerdict(Disposition.ALLOW, risk, reason=why, rule="risk.replay")
        if step_approved_risky and capability_approved:
            return PolicyVerdict(
                Disposition.ALLOW,
                risk,
                reason=f"approved irreversible step ({why})",
                rule="risk.replay",
            )
        return PolicyVerdict(
            Disposition.ESCALATE,
            risk,
            reason=(
                f"irreversible action ({why}) on a capability that is not approved for "
                "unattended execution"
            ),
            rule="risk.replay",
        )
