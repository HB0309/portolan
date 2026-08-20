"""What a run returns to its caller.

The central decision here is that a replay has *three* outcomes, not two:

    success           -- the goal was met; typed outputs are attached
    business_outcome  -- the application gave a legitimate answer that happens
                         not to be the happy path ("no such member")
    failure           -- the automation could not do its job

A caller distinguishes them by reading ``status``, never by parsing a message.
Collapsing the middle case into ``failure`` is the mistake this contract exists
to prevent: it turns a normal answer the end user is waiting for into a page for
an on-call engineer, and it makes the capability unusable by an agent that needs
to say "that member does not exist" out loud.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.schema.capability import RiskClass


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"


class FailureCategory(str, Enum):
    """Why the automation could not proceed.

    Split finely enough that the category alone tells an engineer which part of
    the system to look at: targeting, timing, contract, policy, or the app.
    """

    INPUT_INVALID = "input_invalid"
    ELEMENT_NOT_FOUND = "element_not_found"
    ELEMENT_AMBIGUOUS = "element_ambiguous"
    CHECKPOINT_VIOLATED = "checkpoint_violated"
    TIMEOUT = "timeout"
    POLICY_DENIED = "policy_denied"
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    SESSION_UNRECOVERABLE = "session_unrecoverable"
    SURFACE_ERROR = "surface_error"
    HUMAN_ABORTED = "human_aborted"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    DEAD_END = "dead_end"


class StepStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"  # executed, but resolved below the step's confidence floor
    RECOVERED = "recovered"  # a recovery policy fired and the step then succeeded
    SKIPPED = "skipped"
    FAILED = "failed"


class EvidenceRef(Strict):
    """Pointer to something on disk under evidence/<run_id>/."""

    kind: Literal["screenshot", "dom_snapshot", "a11y_tree", "log", "transcript"]
    path: str
    note: str = ""


class Resolution(Strict):
    """How a descriptor was matched, and how much that should be trusted.

    ``rung`` is the drift telemetry. A tenant whose steps increasingly resolve on
    lower rungs is diverging from the recorded flow, which is the signal to
    review or re-record before it breaks outright.
    """

    rung: int = Field(description="1 is an exact role+name match; 7 is bounding box.")
    rung_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    candidates_considered: int = 1
    degraded: bool = False


class StepTrace(Strict):
    step_id: str
    intent: str
    action_type: str
    status: StepStatus
    target: str | None = Field(default=None, description="Human-readable descriptor.")
    value: str | None = Field(
        default=None,
        description="Log-safe rendering. Parameter references stay unresolved.",
    )
    resolution: Resolution | None = None
    checkpoint_id: str | None = None
    checkpoint_passed: bool | None = None
    risk: RiskClass = RiskClass.SAFE
    duration_ms: int = 0
    attempts: int = 1
    note: str = ""


class RecoveryTrace(Strict):
    policy_id: str
    step_id: str
    strategy: str
    attempts: int
    succeeded: bool
    note: str = ""


class EscalationTrace(Strict):
    intervention_id: str
    step_id: str | None
    reason: str
    raised_at: datetime
    resolved_at: datetime | None = None
    resume_mode: str | None = None
    human_actions_recorded: int = 0


class OutcomeReport(Strict):
    """A declared business outcome fired. This is an answer, not an error."""

    code: str
    description: str
    detected_at_step: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)


class FailureReport(Strict):
    """Everything an engineer needs to debug without re-running.

    The trio of step / expected / observed is deliberate: a failure that says
    only "element not found" costs an hour; one that says which step, what the
    recorded descriptor was, and what was actually on the page costs a minute.
    """

    category: FailureCategory
    step_id: str | None = None
    step_intent: str | None = None
    expected: str = ""
    observed: str = ""
    message: str = ""
    evidence: list[EvidenceRef] = Field(default_factory=list)
    escalated: bool = False


class ReplayResult(Strict):
    status: ReplayStatus
    capability_id: str
    capability_version: int
    run_id: str
    tenant_id: str | None = None
    provider_calls: int = Field(
        default=0,
        description=(
            "Must be 0. Replay is the no-model path; this is asserted at the end "
            "of every run rather than left as a convention."
        ),
    )
    outputs: dict[str, Any] | None = None
    outcome: OutcomeReport | None = None
    failure: FailureReport | None = None
    steps: list[StepTrace] = Field(default_factory=list)
    recoveries: list[RecoveryTrace] = Field(default_factory=list)
    escalations: list[EscalationTrace] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        """True when the automation worked -- including when the answer was 'no'."""
        return self.status in (ReplayStatus.SUCCESS, ReplayStatus.BUSINESS_OUTCOME)

    def summary_line(self) -> str:
        if self.status is ReplayStatus.SUCCESS:
            return f"success: {self.outputs or {}}"
        if self.status is ReplayStatus.BUSINESS_OUTCOME and self.outcome:
            return f"business outcome: {self.outcome.code} -- {self.outcome.description}"
        if self.failure:
            where = f" at step {self.failure.step_id}" if self.failure.step_id else ""
            return f"failure ({self.failure.category.value}){where}: {self.failure.message}"
        return self.status.value

    def degraded_steps(self) -> list[StepTrace]:
        """Steps that executed on a low resolution rung -- the drift signal."""
        return [s for s in self.steps if s.status is StepStatus.DEGRADED]


class DiscoveryResult(Strict):
    """The outcome of an LLM-driven exploration run."""

    succeeded: bool
    run_id: str
    goal: str
    provider: str
    model: str
    steps_taken: int = 0
    provider_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    capability_path: str | None = None
    capability_id: str | None = None
    failure: FailureReport | None = None
    escalations: list[EscalationTrace] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int = 0

    def summary_line(self) -> str:
        if self.succeeded:
            return (
                f"discovered {self.capability_id} in {self.steps_taken} steps "
                f"({self.provider_calls} model calls)"
            )
        reason = self.failure.message if self.failure else "unknown"
        return f"discovery failed after {self.steps_taken} steps: {reason}"
