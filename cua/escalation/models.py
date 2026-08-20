"""What gets routed to a human, and what they send back.

The intervention request carries enough context to act on without going and
looking: which capability, which step and why it exists, what was expected,
what was actually there, a screenshot, and the URL. An escalation that says only
"replay failed" makes the operator redo the diagnosis the system already did.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class InterventionStatus(str, Enum):
    OPEN = "open"
    #: A human has the live session.
    IN_CONTROL = "in_control"
    RESOLVED = "resolved"


class ResumeMode(str, Enum):
    """What the operator wants to happen next.

    ``approve`` and ``performed_manually`` are different on purpose: the first
    says "go ahead and do it", the second says "I already did it, carry on from
    here". Collapsing them would either repeat an irreversible action or skip
    one that never happened.
    """

    APPROVE = "approve"
    PERFORMED_MANUALLY = "performed_manually"
    RETRY_STEP = "retry_step"
    SKIP_STEP = "skip_step"
    ABORT = "abort"


class InterventionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"iv-{uuid.uuid4().hex[:8]}")
    run_id: str
    reason: str
    capability: str | None = None
    goal: str | None = None
    step_id: str | None = None
    step_intent: str | None = None
    expected: str = ""
    observed: str = ""
    url: str = ""
    screenshot: str = ""
    #: What the operator is allowed to answer with, given why we stopped.
    resume_options: list[ResumeMode] = Field(default_factory=list)
    status: InterventionStatus = InterventionStatus.OPEN
    raised_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    resolution: ResumeMode | None = None
    operator_note: str = ""
    human_actions: int = 0

    def context_lines(self) -> list[tuple[str, str]]:
        pairs = [
            ("Capability", self.capability or "-"),
            ("Goal", self.goal or "-"),
            ("Step", f"{self.step_id or '-'} - {self.step_intent or ''}"),
            ("Why we stopped", self.reason),
            ("Expected", self.expected or "-"),
            ("Observed", self.observed or "-"),
            ("URL", self.url or "-"),
        ]
        return [(k, v) for k, v in pairs if v not in ("-", "")]


def options_for(reason: str) -> list[ResumeMode]:
    """Which answers make sense for why we stopped.

    Offering every option every time invites the wrong one. There is no sense in
    "retry" for an irreversible action awaiting approval, and no sense in
    "approve" for a control that could not be found.
    """
    if reason == "irreversible_action":
        return [
            ResumeMode.APPROVE,
            ResumeMode.PERFORMED_MANUALLY,
            ResumeMode.SKIP_STEP,
            ResumeMode.ABORT,
        ]
    if reason in {"element_not_found", "element_ambiguous", "checkpoint_violated"}:
        return [
            ResumeMode.PERFORMED_MANUALLY,
            ResumeMode.RETRY_STEP,
            ResumeMode.SKIP_STEP,
            ResumeMode.ABORT,
        ]
    if reason in {"session_expired", "recovery_escalation"}:
        return [ResumeMode.PERFORMED_MANUALLY, ResumeMode.RETRY_STEP, ResumeMode.ABORT]
    if reason == "agent_stuck":
        return [ResumeMode.PERFORMED_MANUALLY, ResumeMode.ABORT]
    return [ResumeMode.PERFORMED_MANUALLY, ResumeMode.RETRY_STEP, ResumeMode.ABORT]


class OperatorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ResumeMode
    note: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)
