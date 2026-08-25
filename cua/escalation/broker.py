"""Routing a stuck run to a person, and waiting for their answer.

The broker is the join between the run and the operator console. A run raises a
request and awaits it; the console lists open requests and answers them. Because
both live in one process, "await the operator" is an asyncio future rather than
a queue and a poller -- the simplest thing that is actually real.

That simplicity is a deliberate boundary, not an oversight. Everything the
distributed version would need is already here: requests are typed and
serialisable, they carry a run id, and the answer is a small value object. What
would change is where the future is parked, which is exactly the sort of thing
not worth building ahead of a real distributed deployment.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from cua.escalation.models import (
    InterventionRequest,
    InterventionStatus,
    OperatorDecision,
    ResumeMode,
    options_for,
)
from cua.evidence.recorder import EvidenceRecorder
from cua.session.control import ControlOwner
from cua.session.manager import SessionManager


class EscalationBroker:
    def __init__(
        self,
        session: SessionManager,
        evidence: EvidenceRecorder,
        *,
        run_id: str,
        auto_responder: Callable[[InterventionRequest], OperatorDecision] | None = None,
        attended: bool = False,
        timeout_seconds: float = 900.0,
    ) -> None:
        self.session = session
        self.evidence = evidence
        self.run_id = run_id
        #: Used by tests and the unattended demo path. In an attended run this is
        #: None and a real person answers through the console.
        self.auto_responder = auto_responder
        #: Whether anyone is actually watching. An unattended run still *raises*
        #: interventions -- they are recorded, and they are what the operator
        #: queue would be fed from -- but it does not wait on one, because
        #: blocking for fifteen minutes on an operator who was never coming is a
        #: worse outcome than failing fast with a debuggable report.
        self.attended = attended
        self.timeout_seconds = timeout_seconds
        self.requests: dict[str, InterventionRequest] = {}
        self._futures: dict[str, asyncio.Future[OperatorDecision]] = {}

    @property
    def open_requests(self) -> list[InterventionRequest]:
        return [
            request
            for request in self.requests.values()
            if request.status is not InterventionStatus.RESOLVED
        ]

    # -- the run side ----------------------------------------------------

    async def raise_intervention(
        self, reason: str, context: dict[str, Any]
    ) -> OperatorDecision:
        """Stop, ask a human, and wait. The session stays alive throughout."""
        snapshot = await self.session.snapshot_context(f"intervention-{len(self.requests) + 1}")

        request = InterventionRequest(
            run_id=self.run_id,
            reason=reason,
            capability=context.get("capability"),
            goal=context.get("goal"),
            # Discovery numbers its steps, replay names them; both arrive here.
            step_id=str(context["step"]) if context.get("step") is not None else None,
            step_intent=context.get("intent"),
            expected=str(context.get("expected", "") or context.get("action", "")),
            observed=str(context.get("observed", "") or context.get("reason", "")),
            url=str(context.get("url") or snapshot["url"]),
            screenshot=snapshot["screenshot"],
            resume_options=options_for(reason),
        )
        self.requests[request.id] = request

        await self.session.pause_for_human(reason, {"intervention": request.id})
        self.evidence.log(
            "intervention.raised",
            intervention=request.id,
            reason=reason,
            step=request.step_id,
            expected=request.expected,
            observed=request.observed[:300],
        )
        self.evidence.write_json(f"interventions/{request.id}.json", request)

        if self.auto_responder is not None:
            decision = self.auto_responder(request)
            return await self._settle(request, decision)

        if not self.attended:
            return await self._settle(
                request,
                OperatorDecision(
                    mode=ResumeMode.ABORT,
                    note=(
                        "unattended run: the intervention was recorded but there is no "
                        "operator channel to answer it"
                    ),
                ),
            )

        future: asyncio.Future[OperatorDecision] = asyncio.get_running_loop().create_future()
        self._futures[request.id] = future
        try:
            decision = await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            decision = OperatorDecision(
                mode=ResumeMode.ABORT,
                note=f"no operator responded within {self.timeout_seconds:.0f}s",
            )
        return await self._settle(request, decision)

    async def _settle(
        self, request: InterventionRequest, decision: OperatorDecision
    ) -> OperatorDecision:
        human_actions = 0
        if self.session.owner.value in {"human", "handing_back"}:
            human_actions = await self.session.take_back(decision.note)
        else:
            # Resolved without anyone taking the session -- an approval, or an
            # abort. Control returns to automation directly.
            await self.session.take_back(decision.note)

        request.status = InterventionStatus.RESOLVED
        request.resolution = decision.mode
        request.operator_note = decision.note
        request.human_actions = human_actions
        from datetime import datetime, timezone

        request.resolved_at = datetime.now(timezone.utc)
        self.evidence.write_json(f"interventions/{request.id}.json", request)
        self.evidence.log(
            "intervention.resolved",
            intervention=request.id,
            mode=decision.mode.value,
            note=decision.note,
            human_actions=human_actions,
        )
        return OperatorDecision(
            mode=decision.mode,
            note=decision.note,
            extra={"human_actions": human_actions},
        )

    # -- the operator side -----------------------------------------------

    async def take_control(self, intervention_id: str) -> InterventionRequest:
        request = self.requests[intervention_id]
        if self.session.owner is not ControlOwner.BLOCKED:
            # A duplicate /take POST -- a double-click, a browser resubmitting
            # the request on reload, two requests racing each other. The
            # session control FSM only allows BLOCKED -> HUMAN once; calling
            # hand_to_human() again while already HUMAN raised a
            # ControlViolation straight out to an unhandled 500, which a real
            # operator hit live. This is asyncio-safe without a lock: the
            # mutation inside hand_to_human() (control.grant_to_human()) runs
            # synchronously before its first await, so whichever request gets
            # here first has already flipped the owner by the time a second,
            # concurrently-arriving request reaches this check.
            return request
        await self.session.hand_to_human()
        # Control has genuinely transferred the moment hand_to_human() returns
        # -- flip status immediately, before anything else. It used to be set
        # after the screenshot refresh below, which meant the console's
        # rendered state stayed stale (status still OPEN, "Take control" still
        # showing) for however long that screenshot capture took: a real CDP
        # round trip on top of the watcher install hand_to_human() itself just
        # did, not free. Found live: an operator reloaded mid-request, out of
        # exactly that gap, and landed on a response still built from the
        # pre-handoff state -- correct given what the server actually knew at
        # that instant, but indistinguishable from the click having failed.
        request.status = InterventionStatus.IN_CONTROL
        # The screenshot on this request so far is still the one taken when
        # the escalation was *raised* -- before the operator did anything.
        # Nothing ever refreshed it before D25, so every render kept showing
        # that same pre-handoff frame forever. This capture is what fixes
        # that; it no longer gates the status update above, only the picture.
        snapshot = await self.session.snapshot_context(f"{intervention_id}-control-taken")
        request.screenshot = snapshot["screenshot"]
        request.url = snapshot["url"]
        return request

    def resolve(self, intervention_id: str, decision: OperatorDecision) -> None:
        future = self._futures.pop(intervention_id, None)
        if future is not None and not future.done():
            future.set_result(decision)
