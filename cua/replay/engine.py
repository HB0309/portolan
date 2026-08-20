"""Deterministic replay -- the production execution path.

This is what an AI agent actually invokes. It takes a recorded capability and a
set of typed inputs, drives the application, and returns a structured result.
No model is consulted about anything, and that is enforced rather than intended:
the engine holds no provider, and the result carries a ``provider_calls`` count
that is asserted to be zero before it is returned.

The part worth reading closely is ``_evaluate_state``, which runs after every
step in a fixed order:

    1. business-outcome detectors
    2. recovery triggers
    3. checkpoint assertions

The order is the whole design. A "no member found" screen will fail almost any
checkpoint you write, so if checkpoints are evaluated first, a perfectly ordinary
business answer is reported as a broken automation. Detectors go first so that a
legitimate outcome is recognised as one. Recovery goes second so that an
interstitial is cleared before the page is judged. Only then is it fair to ask
whether we arrived where the recording expected.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from cua.evidence.recorder import EvidenceRecorder
from cua.policy.engine import Disposition, Mode, PolicyEngine
from cua.replay import predicates
from cua.schema.capability import (
    BusinessOutcome,
    Capability,
    ExtractAction,
    InputValidationError,
    NavigateAction,
    PressKeyAction,
    RecoveryPolicy,
    RecoveryStrategy,
    RiskClass,
    SelectOptionAction,
    Step,
    TypeTextAction,
)
from cua.schema.results import (
    FailureCategory,
    FailureReport,
    OutcomeReport,
    RecoveryTrace,
    ReplayResult,
    ReplayStatus,
    StepStatus,
    StepTrace,
)
from cua.surfaces.base import ActionRequest, Observation, Surface, SurfaceError
from cua.targeting.resolver import (
    ResolutionError,
    ResolutionFailureKind,
    ResolvedTarget,
    resolve,
)

#: Called when replay cannot safely continue. Returns a resume instruction.
EscalationHook = Callable[[str, dict[str, Any]], Awaitable["ResumeDecision"]]


@dataclass
class ResumeDecision:
    """What a human decided after taking over.

    ``abort`` is a first-class answer, not a failure to decide: an operator who
    looks at the screen and concludes the run should not continue has given the
    system useful information.
    """

    mode: str = "abort"  # retry_step | skip_step | continue | abort
    note: str = ""
    human_actions: int = 0


class _StateVerdict:
    """Result of looking at the screen after a step."""

    def __init__(
        self,
        outcome: BusinessOutcome | None = None,
        recovery: RecoveryPolicy | None = None,
        checkpoint_failed: str | None = None,
    ) -> None:
        self.outcome = outcome
        self.recovery = recovery
        self.checkpoint_failed = checkpoint_failed


@dataclass
class ReplayEngine:
    surface: Surface
    policy: PolicyEngine
    evidence: EvidenceRecorder
    on_escalation: EscalationHook | None = None
    tenant_id: str | None = None

    _traces: list[StepTrace] = field(default_factory=list, init=False)
    _recoveries: list[RecoveryTrace] = field(default_factory=list, init=False)
    _escalations: list[Any] = field(default_factory=list, init=False)
    _outputs: dict[str, Any] = field(default_factory=dict, init=False)
    #: Kept at zero. Its presence in the result is the assertion that this path
    #: never consults a model.
    provider_calls: int = field(default=0, init=False)

    async def run(self, capability: Capability, inputs: dict[str, Any]) -> ReplayResult:
        started = time.monotonic()
        run_id = self.evidence.run_id

        def finish(
            status: ReplayStatus,
            *,
            outcome: OutcomeReport | None = None,
            failure: FailureReport | None = None,
        ) -> ReplayResult:
            result = ReplayResult(
                status=status,
                capability_id=capability.capability_id,
                capability_version=capability.version,
                run_id=run_id,
                tenant_id=self.tenant_id,
                provider_calls=self.provider_calls,
                outputs=dict(self._outputs) if status is ReplayStatus.SUCCESS else None,
                outcome=outcome,
                failure=failure,
                steps=list(self._traces),
                recoveries=list(self._recoveries),
                escalations=list(self._escalations),
                evidence=list(self.evidence.refs),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            # Replay is the no-model path. This is checked, not assumed.
            assert result.provider_calls == 0, "replay must not consult a model"
            self.evidence.log("replay.finished", status=status.value,
                              summary=result.summary_line())
            self.evidence.write_json("result.json", result)
            return result

        self.evidence.log(
            "replay.start",
            capability=capability.slug,
            tenant=self.tenant_id,
            declared_inputs=sorted(i.name for i in capability.inputs),
        )

        # Validate before the browser opens. A caller that sent the wrong shape
        # should learn that in milliseconds, not after a page load.
        try:
            validated = capability.validate_inputs(inputs)
        except InputValidationError as exc:
            return finish(
                ReplayStatus.FAILURE,
                failure=FailureReport(
                    category=FailureCategory.INPUT_INVALID,
                    expected="inputs matching the capability's declared signature",
                    observed=str(exc),
                    message=str(exc),
                ),
            )

        self.policy.redactor.register_values(
            validated[name] for name in capability.sensitive_input_names() if name in validated
        )

        entry = capability.surface.entry_point
        allowed, why = self.policy.url_allowed(entry)
        if not allowed:
            return finish(
                ReplayStatus.FAILURE,
                failure=FailureReport(
                    category=FailureCategory.POLICY_DENIED,
                    expected="an entry point inside the allowlist",
                    observed=entry,
                    message=why,
                ),
            )

        try:
            await self.surface.act(ActionRequest(type="navigate", url=entry))
        except SurfaceError as exc:
            return finish(
                ReplayStatus.FAILURE,
                failure=FailureReport(
                    category=FailureCategory.SURFACE_ERROR,
                    expected=f"the application to load at {entry}",
                    observed=str(exc),
                    message=str(exc),
                ),
            )

        for step in capability.steps:
            outcome_report, failure = await self._run_step(capability, step, validated)
            if outcome_report is not None:
                return finish(ReplayStatus.BUSINESS_OUTCOME, outcome=outcome_report)
            if failure is not None:
                await self.evidence.snapshot(self.surface, f"{step.id}-failure")
                await self.evidence.screenshot(self.surface, f"{step.id}-failure")
                failure.evidence = list(self.evidence.refs)
                return finish(ReplayStatus.FAILURE, failure=failure)

        missing = [o.name for o in capability.outputs if o.name not in self._outputs]
        if missing:
            return finish(
                ReplayStatus.FAILURE,
                failure=FailureReport(
                    category=FailureCategory.CHECKPOINT_VIOLATED,
                    expected=f"every declared output to be produced: {missing}",
                    observed=f"produced {sorted(self._outputs)}",
                    message=f"the flow completed without producing {', '.join(missing)}",
                ),
            )

        await self.evidence.screenshot(self.surface, "final")
        return finish(ReplayStatus.SUCCESS)

    # -- one step --------------------------------------------------------

    async def _run_step(
        self, capability: Capability, step: Step, inputs: dict[str, Any]
    ) -> tuple[OutcomeReport | None, FailureReport | None]:
        attempts = 0
        recovery_budget = {policy.id: policy.max_attempts for policy in capability.recovery}

        while True:
            attempts += 1
            began = time.monotonic()

            observation = await self.surface.observe()

            # Before acting: a known business outcome may already be on screen
            # from the previous step, and a blocking interstitial has to go.
            pre = self._evaluate_state(capability, step, observation, check_checkpoint=False)
            if pre.outcome is not None:
                return self._as_outcome(pre.outcome, step), None
            if pre.recovery is not None:
                recovered, note = await self._recover(
                    capability, step, pre.recovery, recovery_budget, observation
                )
                if recovered:
                    continue
                return None, FailureReport(
                    category=FailureCategory.RECOVERY_EXHAUSTED,
                    step_id=step.id,
                    step_intent=step.intent,
                    expected=f"recovery {pre.recovery.id!r} to clear the condition",
                    observed=note,
                    message=note,
                )

            target: ResolvedTarget | None = None
            if step.target is not None:
                try:
                    target = resolve(
                        step.target,
                        observation,
                        allow_bbox=self.policy.allow_bbox_fallback,
                        min_confidence=step.min_confidence,
                    )
                except ResolutionError as exc:
                    category = (
                        FailureCategory.ELEMENT_AMBIGUOUS
                        if exc.kind is ResolutionFailureKind.AMBIGUOUS
                        else FailureCategory.ELEMENT_NOT_FOUND
                    )
                    decision = await self._escalate(
                        category.value,
                        {
                            "capability": capability.slug,
                            "step": step.id,
                            "intent": step.intent,
                            "expected": step.target.describe(),
                            "observed": exc.observed(),
                            "url": observation.url,
                        },
                    )
                    if decision.mode == "retry_step":
                        continue
                    if decision.mode == "skip_step":
                        self._traces.append(
                            self._trace(step, StepStatus.SKIPPED, began,
                                        note="skipped by operator")
                        )
                        return None, None
                    return None, FailureReport(
                        category=category,
                        step_id=step.id,
                        step_intent=step.intent,
                        expected=step.target.describe(),
                        observed=exc.observed(),
                        message=str(exc),
                        escalated=bool(self._escalations),
                    )

            verdict = self.policy.check(
                mode=Mode.REPLAY,
                action_type=step.action.type,
                url=(
                    step.action.url.render(inputs)
                    if isinstance(step.action, NavigateAction)
                    else None
                ),
                target=step.target,
                step_approved_risky=step.approved_risky,
                capability_approved=capability.approval == "approved",
            )

            if verdict.disposition is Disposition.DENY:
                return None, FailureReport(
                    category=FailureCategory.POLICY_DENIED,
                    step_id=step.id,
                    step_intent=step.intent,
                    expected="an action permitted by policy",
                    observed=verdict.reason,
                    message=verdict.reason,
                )

            if verdict.disposition is Disposition.ESCALATE:
                decision = await self._escalate(
                    "irreversible_action",
                    {
                        "capability": capability.slug,
                        "step": step.id,
                        "intent": step.intent,
                        "action": step.action.type,
                        "target": step.target.describe() if step.target else None,
                        "reason": verdict.reason,
                        "url": observation.url,
                    },
                )
                if decision.mode == "abort":
                    return None, FailureReport(
                        category=FailureCategory.HUMAN_ABORTED,
                        step_id=step.id,
                        step_intent=step.intent,
                        expected="operator approval for an irreversible action",
                        observed=decision.note or "operator declined",
                        message=verdict.reason,
                        escalated=True,
                    )
                if decision.mode == "skip_step":
                    self._traces.append(
                        self._trace(step, StepStatus.SKIPPED, began,
                                    note="skipped by operator")
                    )
                    return None, None
                if decision.mode == "retry_step":
                    continue
                # "continue" means the human performed the action themselves or
                # approved it; re-observe rather than assuming the screen is
                # where we left it.
                if decision.human_actions:
                    self._traces.append(
                        self._trace(step, StepStatus.RECOVERED, began,
                                    note=f"performed by operator ({decision.note})")
                    )
                    return None, None

            try:
                result = await self._perform(step, target, inputs)
            except SurfaceError as exc:
                if attempts <= 2:
                    self.evidence.log("replay.retrying", step=step.id, error=str(exc))
                    await asyncio.sleep(0.5)
                    continue
                return None, FailureReport(
                    category=FailureCategory.SURFACE_ERROR,
                    step_id=step.id,
                    step_intent=step.intent,
                    expected=f"to {step.action.type} {step.target.describe() if step.target else ''}",
                    observed=str(exc),
                    message=str(exc),
                )

            if isinstance(step.action, ExtractAction):
                self._outputs[step.action.output] = result

            after = await self.surface.observe()

            # Recovery *after* the action must not re-run the action. The step
            # already happened; an interstitial that appeared on the way to the
            # next screen needs clearing, not a second click. Retrying here is
            # how a dismissed notice turns into a double submission -- or, more
            # visibly, into looking for the Search button on a page that has
            # already moved past it.
            post = self._evaluate_state(capability, step, after, check_checkpoint=True)
            while post.recovery is not None:
                recovered, note = await self._recover(
                    capability, step, post.recovery, recovery_budget, after
                )
                if not recovered:
                    return None, FailureReport(
                        category=FailureCategory.RECOVERY_EXHAUSTED,
                        step_id=step.id,
                        step_intent=step.intent,
                        expected=f"recovery {post.recovery.id!r} to clear the condition",
                        observed=note,
                        message=note,
                    )
                after = await self.surface.observe()
                post = self._evaluate_state(capability, step, after, check_checkpoint=True)

            if post.outcome is not None:
                return self._as_outcome(post.outcome, step), None

            if post.checkpoint_failed is not None:
                decision = await self._escalate(
                    "checkpoint_violated",
                    {
                        "capability": capability.slug,
                        "step": step.id,
                        "intent": step.intent,
                        "expected": post.checkpoint_failed,
                        "observed": (after.text or "")[:400],
                        "url": after.url,
                    },
                )
                if decision.mode == "retry_step":
                    continue
                if decision.mode in {"skip_step", "continue"}:
                    self._traces.append(
                        self._trace(step, StepStatus.RECOVERED, began, target=target,
                                    note="checkpoint waived by operator")
                    )
                    return None, None
                return None, FailureReport(
                    category=FailureCategory.CHECKPOINT_VIOLATED,
                    step_id=step.id,
                    step_intent=step.intent,
                    expected=post.checkpoint_failed,
                    observed=f"url={after.url} :: {(after.text or '')[:200]}",
                    message=(
                        f"step {step.id} completed but the expected state was not "
                        f"reached"
                    ),
                    escalated=bool(self._escalations),
                )

            status = (
                StepStatus.DEGRADED
                if target and target.resolution.degraded
                else StepStatus.OK
            )
            self._traces.append(
                self._trace(step, status, began, target=target, attempts=attempts,
                            value=self._display_value(step))
            )
            await self.evidence.screenshot(self.surface, f"{step.id}")
            return None, None

    # -- helpers ---------------------------------------------------------

    async def _perform(
        self, step: Step, target: ResolvedTarget | None, inputs: dict[str, Any]
    ) -> str | None:
        action = step.action
        ref = target.element.ref if target else None

        if isinstance(action, NavigateAction):
            await self.surface.act(
                ActionRequest(type="navigate", url=action.url.render(inputs))
            )
            return None
        if isinstance(action, TypeTextAction):
            await self.surface.act(
                ActionRequest(type="type_text", ref=ref, text=action.value.render(inputs))
            )
            return None
        if isinstance(action, SelectOptionAction):
            await self.surface.act(
                ActionRequest(type="select_option", ref=ref, option=action.value.render(inputs))
            )
            return None
        if isinstance(action, PressKeyAction):
            await self.surface.act(ActionRequest(type="press_key", ref=ref, key=action.key))
            return None
        if isinstance(action, ExtractAction):
            outcome = await self.surface.act(ActionRequest(type="extract", ref=ref))
            value = (outcome.extracted or "").strip()
            if action.pattern:
                import re

                match = re.search(action.pattern, value)
                if match:
                    value = match.group(1) if match.groups() else match.group(0)
            return value
        await self.surface.act(ActionRequest(type=action.type, ref=ref))
        return None

    def _evaluate_state(
        self,
        capability: Capability,
        step: Step,
        observation: Observation,
        *,
        check_checkpoint: bool,
    ) -> _StateVerdict:
        """The fixed evaluation order. See the module docstring."""
        for outcome in capability.outcomes:
            if predicates.evaluate(outcome.detector, observation):
                return _StateVerdict(outcome=outcome)

        for policy in capability.recovery:
            if policy.id not in step.recovery_ids:
                continue
            if predicates.evaluate(policy.trigger, observation):
                return _StateVerdict(recovery=policy)

        if check_checkpoint and step.checkpoint is not None:
            for assertion in step.checkpoint.assertions:
                if not predicates.evaluate(assertion, observation):
                    return _StateVerdict(checkpoint_failed=predicates.describe(assertion))

        return _StateVerdict()

    async def _recover(
        self,
        capability: Capability,
        step: Step,
        policy: RecoveryPolicy,
        budget: dict[str, int],
        observation: Observation,
    ) -> tuple[bool, str]:
        remaining = budget.get(policy.id, 0)
        if remaining <= 0:
            note = f"recovery {policy.id!r} exhausted its {policy.max_attempts} attempts"
            self._recoveries.append(
                RecoveryTrace(policy_id=policy.id, step_id=step.id,
                              strategy=policy.strategy.value, attempts=policy.max_attempts,
                              succeeded=False, note=note)
            )
            return False, note

        budget[policy.id] = remaining - 1
        used = policy.max_attempts - remaining + 1
        self.evidence.log("recovery.applied", step=step.id, policy=policy.id,
                          strategy=policy.strategy.value, attempt=used)

        try:
            if policy.strategy is RecoveryStrategy.WAIT:
                await asyncio.sleep(policy.wait_ms / 1000)
            elif policy.strategy is RecoveryStrategy.DISMISS:
                if policy.target is None:
                    return False, f"recovery {policy.id!r} has no control to dismiss with"
                found = resolve(policy.target, observation)
                await self.surface.act(ActionRequest(type="click", ref=found.element.ref))
            elif policy.strategy is RecoveryStrategy.REAUTH:
                # Re-authentication is deliberately not automated here: doing it
                # would mean this process holding operator credentials, which is
                # exactly what the safety model says it must not do. It routes to
                # a human instead, who signs in on the live session.
                decision = await self._escalate(
                    "session_expired",
                    {
                        "capability": capability.slug,
                        "step": step.id,
                        "reason": policy.description,
                        "url": observation.url,
                    },
                )
                if decision.mode == "abort":
                    return False, "operator declined to re-authenticate"
            elif policy.strategy is RecoveryStrategy.ESCALATE:
                decision = await self._escalate(
                    "recovery_escalation",
                    {"capability": capability.slug, "step": step.id,
                     "reason": policy.description, "url": observation.url},
                )
                if decision.mode == "abort":
                    return False, "operator aborted"
        except (SurfaceError, ResolutionError) as exc:
            return False, f"recovery {policy.id!r} failed: {exc}"

        self._recoveries.append(
            RecoveryTrace(policy_id=policy.id, step_id=step.id,
                          strategy=policy.strategy.value, attempts=used, succeeded=True,
                          note=policy.description)
        )
        return True, ""

    async def _escalate(self, reason: str, context: dict[str, Any]) -> ResumeDecision:
        self.evidence.log("escalation.raised", reason=reason, context=context)
        await self.evidence.screenshot(self.surface, f"escalation-{len(self._escalations) + 1}")
        if self.on_escalation is None:
            return ResumeDecision(mode="abort", note="no operator channel configured")
        decision = await self.on_escalation(reason, context)
        self.evidence.log("escalation.resolved", reason=reason, mode=decision.mode,
                          note=decision.note, human_actions=decision.human_actions)
        return decision

    def _as_outcome(self, outcome: BusinessOutcome, step: Step) -> OutcomeReport:
        self.evidence.log("outcome.detected", code=outcome.code, step=step.id)
        return OutcomeReport(
            code=outcome.code,
            description=outcome.description,
            detected_at_step=step.id,
            outputs=dict(self._outputs),
        )

    def _display_value(self, step: Step) -> str | None:
        value = getattr(step.action, "value", None)
        return value.display() if value is not None else None

    def _trace(
        self,
        step: Step,
        status: StepStatus,
        began: float,
        *,
        target: ResolvedTarget | None = None,
        attempts: int = 1,
        note: str = "",
        value: str | None = None,
    ) -> StepTrace:
        return StepTrace(
            step_id=step.id,
            intent=step.intent,
            action_type=step.action.type,
            status=status,
            target=step.target.describe() if step.target else None,
            value=value,
            resolution=target.resolution if target else None,
            checkpoint_id=step.checkpoint.id if step.checkpoint else None,
            checkpoint_passed=True if step.checkpoint else None,
            risk=step.risk if isinstance(step.risk, RiskClass) else RiskClass.SAFE,
            duration_ms=int((time.monotonic() - began) * 1000),
            attempts=attempts,
            note=note,
        )
