"""The live session, and the seam a human takes it over through.

One browser context lives for the whole run. The session manager owns it, hands
the surface a control check, and records what a human does while they hold
control.

Recording the human's actions matters for a reason beyond the audit trail: a
capability that needed manual help is a capability with a gap in it, and the
captured actions are the raw material for closing that gap in the next version.
An intervention that teaches the system nothing will happen again tomorrow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cua.evidence.recorder import EvidenceRecorder
from cua.session.control import ControlOwner, ControlToken
from cua.surfaces.web import WebSurface

#: Injected into the page while a human holds control. Reports what they do back
#: to the recorder. Values are never sent -- only that a field was filled, and
#: which one -- because the operator may be typing regulated data.
_WATCHER_JS = """
(() => {
  if (window.__cuaWatching) return;
  window.__cuaWatching = true;
  const describe = (el) => {
    if (!el || !el.tagName) return "unknown";
    const tag = el.tagName.toLowerCase();
    const name = el.getAttribute("name") || el.getAttribute("value") || "";
    const text = (el.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 40);
    return `${tag}${name ? `[${name}]` : ""}${text ? ` "${text}"` : ""}`;
  };
  document.addEventListener("click", (e) => {
    window.__cuaHumanAction("click", describe(e.target), "");
  }, true);
  document.addEventListener("change", (e) => {
    window.__cuaHumanAction("change", describe(e.target), "(value withheld)");
  }, true);
})();
"""


@dataclass
class HumanAction:
    kind: str
    target: str
    detail: str = ""
    url: str = ""


@dataclass
class SessionManager:
    surface: WebSurface
    evidence: EvidenceRecorder
    control: ControlToken = field(default_factory=ControlToken)
    human_actions: list[HumanAction] = field(default_factory=list)
    _watching: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.surface.bind_control_check(self.control.assert_automation)

    @property
    def owner(self) -> ControlOwner:
        return self.control.owner

    # -- handoff ---------------------------------------------------------

    async def pause_for_human(self, reason: str, context: dict[str, Any]) -> None:
        """Stop automating. The session stays exactly where it is."""
        self.control.block()
        self.evidence.log(
            "control.blocked",
            reason=reason,
            owner=self.control.owner.value,
            context=context,
        )

    async def hand_to_human(self) -> None:
        """Give a person the live session and start watching what they do."""
        self.control.grant_to_human()
        await self._install_watcher()
        self.evidence.log("control.granted", owner=self.control.owner.value)

    async def take_back(self, note: str = "") -> int:
        """Take control back, after the human says they are done.

        Returns how many actions they took. The automation still has to
        re-observe and re-check its expectations before acting -- this only
        restores the right to act, not the assumption that nothing changed.
        """
        count = len(self.human_actions)
        self.control.restore_to_automation()
        self.evidence.log(
            "control.returned",
            owner=self.control.owner.value,
            human_actions=count,
            note=note,
        )
        return count

    async def _install_watcher(self) -> None:
        if self._watching:
            return
        page = self.surface.page

        async def report(_source: Any, kind: str, target: str, detail: str) -> None:
            action = HumanAction(kind=kind, target=target, detail=detail, url=page.url)
            self.human_actions.append(action)
            self.control.record_human_action()
            self.evidence.log(
                "human.action", kind=kind, target=target, detail=detail, url=page.url
            )

        try:
            await page.expose_binding("__cuaHumanAction", report)
            await page.add_init_script(_WATCHER_JS)
            await page.evaluate(_WATCHER_JS)
            self._watching = True
        except Exception as exc:  # pragma: no cover - best effort
            # Losing the watcher costs an audit detail, not the handoff itself.
            self.evidence.log("human.watch_failed", error=str(exc))

    async def snapshot_context(self, label: str) -> dict[str, str]:
        """Everything an operator needs to understand the situation."""
        screenshot = await self.evidence.screenshot(self.surface, label)
        return {"screenshot": screenshot, "url": await self.surface.current_url()}
