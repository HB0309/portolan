"""The discovery loop.

Observe, decide, act, until the goal is met or a stopping condition fires. This
is the expensive path: it runs once per capability, with a model in the loop,
and everything it learns is handed to the recorder to be turned into something
that never needs the model again.

The loop keeps one invariant that is easy to lose sight of: **the model proposes,
the runtime disposes.** Every action it asks for is checked against policy before
it reaches the surface, and a refusal is fed back to the agent as an observation
rather than being quietly dropped. An agent that is told "that control commits an
irreversible change and needs a human" can do something sensible with that. An
agent whose action silently does nothing will simply try it again.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from cua.discovery.prompts import (
    SYSTEM_PROMPT,
    render_goal,
    render_observation,
    summarize_step,
)
from cua.discovery.tools import ACTION_TOOLS, TERMINAL_TOOLS, tool_definitions
from cua.evidence.recorder import EvidenceRecorder
from cua.llm.base import LLMError, LLMProvider, Message
from cua.policy.engine import Disposition, Mode, PolicyEngine
from cua.schema.capability import ElementDescriptor
from cua.schema.results import DiscoveryResult, FailureCategory, FailureReport
from cua.surfaces.base import ActionRequest, Element, Observation, Surface, SurfaceError

@dataclass
class InterventionOutcome:
    """How an escalation was resolved.

    ``approved`` alone used to be the whole signal, and that collapsed two
    different answers into one: "go ahead and do this yourself" and "I already
    did this myself, don't do it again" both came back as ``True``. The
    difference matters more than almost anything else in this system --
    conflating them meant a human clicking the real "Open Sub-Account" button
    during a handoff was followed by the automation clicking the same button a
    second time, on a page it no longer even matched. ``performed_by_human``
    is what lets the caller skip re-acting.
    """

    approved: bool
    performed_by_human: bool = False


#: Signature of the human-escalation hook.
InterventionHook = Callable[[str, dict[str, Any]], Awaitable[InterventionOutcome]]


@dataclass
class DiscoveryStep:
    """One executed action, with everything the recorder needs to generalise it."""

    index: int
    tool: str
    arguments: dict[str, Any]
    intent: str
    element: Element | None
    observation_before: Observation
    observation_after: Observation | None
    outcome: str
    extracted: str | None = None
    output_name: str | None = None
    navigated: bool = False
    risk: str = "safe"
    human_approved: bool = False


@dataclass
class DiscoveryTrace:
    goal: str
    entry_point: str
    inputs: dict[str, str]
    outputs: list[str]
    steps: list[DiscoveryStep] = field(default_factory=list)
    collected: dict[str, str] = field(default_factory=dict)
    summary: str = ""


class DiscoveryOrchestrator:
    def __init__(
        self,
        surface: Surface,
        provider: LLMProvider,
        policy: PolicyEngine,
        evidence: EvidenceRecorder,
        *,
        on_intervention: InterventionHook | None = None,
    ) -> None:
        self.surface = surface
        self.provider = provider
        self.policy = policy
        self.evidence = evidence
        self.on_intervention = on_intervention
        self.provider_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        #: Total time spent waiting on a human to answer an escalation, across
        #: the run. Excluded from the wall-clock budget below -- see
        #: _raise_intervention for why.
        self._human_wait_seconds = 0.0

    async def run(
        self,
        *,
        goal: str,
        entry_point: str,
        inputs: dict[str, str],
        outputs: list[str],
    ) -> tuple[DiscoveryResult, DiscoveryTrace]:
        run_id = self.evidence.run_id
        started = time.monotonic()
        trace = DiscoveryTrace(
            goal=goal, entry_point=entry_point, inputs=dict(inputs), outputs=list(outputs)
        )
        # Sensitive caller values are registered before anything is written, so
        # they cannot reach a log even by an unexpected route.
        self.policy.redactor.register_values(inputs.values())

        self.evidence.log(
            "discovery.start",
            goal=goal,
            entry_point=entry_point,
            provider=self.provider.name,
            model=self.provider.model,
            declared_inputs=sorted(inputs),
            declared_outputs=outputs,
        )

        allowed, why = self.policy.url_allowed(entry_point)
        if not allowed:
            return self._failed(
                trace, run_id, started, FailureCategory.POLICY_DENIED, f"entry point: {why}"
            )

        try:
            await self.surface.act(ActionRequest(type="navigate", url=entry_point))
        except SurfaceError as exc:
            return self._failed(
                trace, run_id, started, FailureCategory.SURFACE_ERROR, str(exc)
            )

        observation = await self.surface.observe()
        history: list[str] = []
        consecutive_failures = 0
        limits = self.policy.limits
        deadline = started + limits.wall_clock_seconds
        # Static for the whole run -- rebuilding this nested structure on every
        # step (up to max_steps times) recomputed the same nothing each time.
        tools = tool_definitions()

        for step_index in range(1, limits.max_steps + 1):
            if time.monotonic() > deadline + self._human_wait_seconds:
                return self._failed(
                    trace,
                    run_id,
                    started,
                    FailureCategory.TIMEOUT,
                    f"exceeded the {limits.wall_clock_seconds}s wall clock",
                )

            await self.evidence.screenshot(self.surface, f"{step_index:02d}-before")

            prompt = "\n".join(
                [
                    render_goal(goal, inputs, outputs),
                    ("What you have done so far:\n" + "\n".join(history) + "\n")
                    if history
                    else "",
                    render_observation(observation),
                ]
            )
            self.evidence.transcript("request", {"step": step_index, "prompt": prompt})

            try:
                response = await self.provider.decide(
                    SYSTEM_PROMPT, [Message(role="user", content=prompt)], tools
                )
            except LLMError as exc:
                return self._failed(
                    trace, run_id, started, FailureCategory.SURFACE_ERROR, str(exc)
                )

            self.provider_calls += 1
            self.input_tokens += response.input_tokens
            self.output_tokens += response.output_tokens
            self.evidence.transcript(
                "response",
                {
                    "step": step_index,
                    "tool": response.tool_call.name if response.tool_call else None,
                    "arguments": response.tool_call.arguments if response.tool_call else {},
                    "text": response.text,
                },
            )

            call = response.tool_call
            if call is None:
                consecutive_failures += 1
                history.append(f"  {step_index}. (model returned no action)")
                if consecutive_failures >= limits.max_consecutive_noops:
                    return self._failed(
                        trace,
                        run_id,
                        started,
                        FailureCategory.DEAD_END,
                        "the model stopped proposing actions",
                    )
                continue

            self.evidence.log(
                "discovery.decision",
                step=step_index,
                tool=call.name,
                intent=call.reasoning,
                arguments={k: v for k, v in call.arguments.items() if k != "text"},
            )

            if call.name in TERMINAL_TOOLS:
                trace.summary = str(
                    call.arguments.get("summary") or call.arguments.get("reason") or ""
                )
                if call.name == "give_up":
                    await self._raise_intervention(
                        "agent_stuck",
                        {
                            "goal": goal,
                            "step": step_index,
                            "reason": trace.summary,
                            "url": await self.surface.current_url(),
                        },
                    )
                    return self._failed(
                        trace, run_id, started, FailureCategory.DEAD_END, trace.summary
                    )
                missing = [name for name in outputs if name not in trace.collected]
                if missing:
                    history.append(
                        f"  {step_index}. finish refused :: outputs still missing: "
                        f"{', '.join(missing)}"
                    )
                    self.evidence.log("discovery.finish_rejected", missing=missing)
                    continue
                break

            step, feedback = await self._execute(step_index, call, observation, trace)
            history.append(feedback)

            if step is None:
                consecutive_failures += 1
                if consecutive_failures >= limits.max_consecutive_noops:
                    return self._failed(
                        trace,
                        run_id,
                        started,
                        FailureCategory.DEAD_END,
                        f"{consecutive_failures} consecutive actions had no effect",
                    )
                observation = await self.surface.observe()
                continue

            consecutive_failures = 0
            observation = await self.surface.observe()
            step.observation_after = observation
            trace.steps.append(step)
        else:
            return self._failed(
                trace,
                run_id,
                started,
                FailureCategory.MAX_STEPS_EXCEEDED,
                f"reached the {limits.max_steps}-step ceiling without meeting the goal",
            )

        await self.evidence.screenshot(self.surface, "final")
        self.evidence.log(
            "discovery.succeeded",
            steps=len(trace.steps),
            collected=sorted(trace.collected),
            summary=trace.summary,
        )

        return (
            DiscoveryResult(
                succeeded=True,
                run_id=run_id,
                goal=goal,
                provider=self.provider.name,
                model=self.provider.model,
                steps_taken=len(trace.steps),
                provider_calls=self.provider_calls,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                evidence=list(self.evidence.refs),
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
            trace,
        )

    # -- one action ------------------------------------------------------

    async def _execute(
        self,
        step_index: int,
        call: Any,
        observation: Observation,
        trace: DiscoveryTrace,
    ) -> tuple[DiscoveryStep | None, str]:
        action_type = ACTION_TOOLS[call.name]
        arguments = dict(call.arguments)
        ref = arguments.get("ref")

        element: Element | None = None
        if action_type != "navigate":
            element = observation.by_ref(str(ref)) if ref else None
            if element is None:
                return None, summarize_step(
                    step_index, call.name, arguments, f"no element {ref!r} in this observation"
                )
            arguments["target_description"] = element.describe()

        verdict = self.policy.check(
            mode=Mode.DISCOVERY,
            action_type=action_type,
            url=arguments.get("url") if action_type == "navigate" else None,
            target=_provisional_descriptor(element) if element else None,
        )

        human_approved = False
        if verdict.disposition is Disposition.ESCALATE:
            escalation = await self._raise_intervention(
                "irreversible_action",
                {
                    "step": step_index,
                    "action": action_type,
                    "target": element.describe() if element else arguments.get("url"),
                    "reason": verdict.reason,
                    "url": await self.surface.current_url(),
                },
            )
            human_approved = escalation.approved
            if not escalation.approved:
                self.evidence.log(
                    "policy.blocked", step=step_index, reason=verdict.reason, rule=verdict.rule
                )
                return None, summarize_step(
                    step_index,
                    call.name,
                    arguments,
                    f"BLOCKED by policy: {verdict.reason}. A human must approve this.",
                )
            if escalation.performed_by_human:
                # The action already happened, live, at the operator's hand.
                # Acting again would either fail on a stale reference to a
                # control that no longer exists on whatever page the human's
                # click produced, or -- worse, if timing let the old element
                # still resolve -- actually repeat an irreversible transaction.
                # observation_after is left for the caller to fill in with its
                # own re-observe, same as every other step here; there is no
                # reason to perceive the screen twice.
                self.evidence.log(
                    "action.performed_by_operator",
                    step=step_index,
                    action=action_type,
                    target=element.describe() if element else arguments.get("url"),
                )
                step = DiscoveryStep(
                    index=step_index,
                    tool=call.name,
                    arguments=arguments,
                    intent=call.reasoning
                    or f"{call.name} {arguments.get('target_description', '')}",
                    element=element,
                    observation_before=observation,
                    observation_after=None,
                    outcome="performed by the operator during handoff",
                    navigated=True,
                    risk=verdict.risk.value,
                    human_approved=True,
                )
                return step, summarize_step(
                    step_index, call.name, arguments, "performed by the operator"
                )
        elif verdict.disposition is Disposition.DENY:
            self.evidence.log(
                "policy.denied", step=step_index, reason=verdict.reason, rule=verdict.rule
            )
            return None, summarize_step(
                step_index, call.name, arguments, f"DENIED by policy: {verdict.reason}"
            )

        request = ActionRequest(
            type=action_type,
            ref=element.ref if element else None,
            url=arguments.get("url"),
            text=arguments.get("text"),
            key=arguments.get("key"),
            option=arguments.get("option"),
        )

        try:
            result = await self.surface.act(request)
        except SurfaceError as exc:
            self.evidence.log("action.failed", step=step_index, error=str(exc))
            return None, summarize_step(step_index, call.name, arguments, f"failed: {exc}")

        output_name = None
        extracted = None
        if action_type == "extract":
            output_name = str(arguments.get("output", ""))
            extracted = result.extracted or ""
            if output_name not in trace.outputs:
                return None, summarize_step(
                    step_index,
                    call.name,
                    arguments,
                    f"{output_name!r} is not a declared output of this goal",
                )
            trace.collected[output_name] = extracted

        self.evidence.log(
            "action.performed",
            step=step_index,
            action=action_type,
            target=element.describe() if element else arguments.get("url"),
            intent=call.reasoning,
            navigated=result.navigated,
            risk=verdict.risk.value,
            human_approved=human_approved,
        )

        outcome = result.detail
        if extracted is not None:
            outcome = f"read {extracted!r}"

        step = DiscoveryStep(
            index=step_index,
            tool=call.name,
            arguments=arguments,
            intent=call.reasoning or f"{call.name} {arguments.get('target_description', '')}",
            element=element,
            observation_before=observation,
            observation_after=None,
            outcome=outcome,
            extracted=extracted,
            output_name=output_name,
            navigated=result.navigated,
            risk=verdict.risk.value,
            human_approved=human_approved,
        )
        return step, summarize_step(step_index, call.name, arguments, outcome)

    async def _raise_intervention(
        self, reason: str, context: dict[str, Any]
    ) -> InterventionOutcome:
        self.evidence.log("escalation.raised", reason=reason, context=context)
        if self.on_intervention is None:
            return InterventionOutcome(approved=False)
        # The wall-clock budget below exists to catch automation that loops
        # without making progress. A human deciding whether to approve an
        # irreversible action is the opposite of that -- found live, when a
        # real operator took about ten minutes to get back to an escalation
        # (nothing wrong with that; that is what "attended" means) and the
        # run failed on a timeout immediately after they resolved it, having
        # done everything right. The broker already has its own, much longer
        # timeout for an unanswered escalation (900s); this only needs to stop
        # a real human's genuine response time from silently eating the
        # budget meant for the automation loop.
        waited_from = time.monotonic()
        outcome = await self.on_intervention(reason, context)
        self._human_wait_seconds += time.monotonic() - waited_from
        self.evidence.log(
            "escalation.resolved",
            reason=reason,
            approved=outcome.approved,
            performed_by_human=outcome.performed_by_human,
        )
        return outcome

    def _failed(
        self,
        trace: DiscoveryTrace,
        run_id: str,
        started: float,
        category: FailureCategory,
        message: str,
    ) -> tuple[DiscoveryResult, DiscoveryTrace]:
        self.evidence.log("discovery.failed", category=category.value, message=message)
        return (
            DiscoveryResult(
                succeeded=False,
                run_id=run_id,
                goal=trace.goal,
                provider=self.provider.name,
                model=self.provider.model,
                steps_taken=len(trace.steps),
                provider_calls=self.provider_calls,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                failure=FailureReport(category=category, message=message),
                evidence=list(self.evidence.refs),
                started_at=datetime.now(timezone.utc),
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
            trace,
        )


def _provisional_descriptor(element: Element) -> ElementDescriptor:
    """Just enough descriptor for the policy engine to classify reversibility.

    Not the recorded one -- that is built later by the recorder with anchors and
    a robustness note. This exists only so the risk classifier can see what the
    control is called before the action happens.
    """
    return ElementDescriptor(
        role=element.role,
        accessible_name=element.effective_name,
        robustness_note="provisional; used for policy classification only",
    )


def new_run_id(prefix: str = "discovery") -> str:
    return f"{prefix}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
