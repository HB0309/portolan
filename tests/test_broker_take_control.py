"""Ordering inside EscalationBroker.take_control().

Found live: an operator clicked "Take control", the response was slow --
hand_to_human() installs a human-action watcher over CDP, and take_control()
also refreshes the escalation's screenshot, both real round trips, not free --
and they reloaded mid-request out of impatience. Their reload landed on a
render still built from pre-handoff state, because request.status used to
flip to IN_CONTROL only after the screenshot capture finished, well after
control had actually transferred. The render was internally consistent with
what the server knew at that instant, but indistinguishable from the click
having failed.

These tests exercise take_control() against a fake session whose
snapshot_context() is deliberately slow, so the ordering property -- status
becomes correct the moment control transfers, not whenever the picture
happens to catch up -- is directly observable rather than inferred.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from cua.escalation.broker import EscalationBroker
from cua.escalation.models import InterventionStatus, OperatorDecision, ResumeMode
from cua.evidence import EvidenceRecorder
from cua.policy import PolicyEngine
from cua.session.control import ControlOwner


@dataclass
class FakeSession:
    """Just enough of SessionManager's interface for the broker to drive."""

    _owner: ControlOwner = ControlOwner.AUTOMATION
    screenshot_delay: float = 0.0
    snapshot_calls: list[str] = field(default_factory=list)

    @property
    def owner(self) -> ControlOwner:
        return self._owner

    async def pause_for_human(self, reason: str, context: dict) -> None:
        self._owner = ControlOwner.BLOCKED

    async def hand_to_human(self) -> None:
        self._owner = ControlOwner.HUMAN

    async def take_back(self, note: str = "") -> int:
        self._owner = ControlOwner.AUTOMATION
        return 0

    async def snapshot_context(self, label: str) -> dict[str, str]:
        self.snapshot_calls.append(label)
        if self.screenshot_delay:
            await asyncio.sleep(self.screenshot_delay)
        return {"screenshot": f"/tmp/{label}.png", "url": "http://127.0.0.1:8080/"}


def _make_broker(tmp_path, session: FakeSession) -> EscalationBroker:
    policy = PolicyEngine.load()
    evidence = EvidenceRecorder("test-take-control", root=tmp_path, redactor=policy.redactor)
    return EscalationBroker(session, evidence, run_id="test-take-control", attended=True)


class TestStatusFlipsBeforeTheScreenshotRefresh:
    async def test_status_is_in_control_while_the_screenshot_is_still_being_taken(
        self, tmp_path
    ):
        session = FakeSession(screenshot_delay=0.2)
        broker = _make_broker(tmp_path, session)
        decision_task = asyncio.create_task(
            broker.raise_intervention("irreversible_action", {"step": 1})
        )
        # Let raise_intervention actually create and register the request.
        for _ in range(200):
            if broker.open_requests:
                break
            await asyncio.sleep(0.01)
        request = broker.open_requests[0]

        take_task = asyncio.create_task(broker.take_control(request.id))
        # Shorter than screenshot_delay: the screenshot call is still in
        # flight at this point, but control has already transferred.
        await asyncio.sleep(0.05)
        assert session.owner is ControlOwner.HUMAN
        assert request.status is InterventionStatus.IN_CONTROL

        await take_task
        broker.resolve(request.id, OperatorDecision(mode=ResumeMode.PERFORMED_MANUALLY))
        await decision_task

    async def test_the_screenshot_is_still_refreshed_once_it_completes(self, tmp_path):
        session = FakeSession(screenshot_delay=0.01)
        broker = _make_broker(tmp_path, session)
        decision_task = asyncio.create_task(
            broker.raise_intervention("irreversible_action", {"step": 1})
        )
        for _ in range(200):
            if broker.open_requests:
                break
            await asyncio.sleep(0.01)
        request = broker.open_requests[0]
        screenshot_before = request.screenshot

        await broker.take_control(request.id)
        assert request.screenshot != screenshot_before
        assert any("control-taken" in call for call in session.snapshot_calls)

        broker.resolve(request.id, OperatorDecision(mode=ResumeMode.PERFORMED_MANUALLY))
        await decision_task
