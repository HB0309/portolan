"""The console's auto-refresh tag: present only while idle.

Found live: a card's auto-refresh (a 3s meta-refresh, so it caught up promptly
after a new escalation appeared) raced an operator's own click on "Take
control" -- a browser can only run one navigation at a time, and a scheduled
reload landing mid-click abandons the click's own POST before it ever reaches
the server. A human's realistic time to read a card and click is comfortably
inside a 3s window, so this was not a rare coincidence.

These tests check the one thing that matters: the tag is absent whenever a
card is on screen (nothing to interrupt), and present while idle (so a new
escalation is still noticed promptly).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from cua.escalation.broker import EscalationBroker
from cua.escalation.console import build_console
from cua.escalation.models import OperatorDecision, ResumeMode
from cua.evidence import EvidenceRecorder
from cua.policy import PolicyEngine
from cua.session.control import ControlOwner


@dataclass
class FakeSession:
    """Just enough of SessionManager's interface for the broker to drive."""

    _owner: ControlOwner = ControlOwner.AUTOMATION

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
        return {"screenshot": f"/tmp/{label}.png", "url": "http://127.0.0.1:8080/"}


def _make_broker(tmp_path, session: FakeSession) -> EscalationBroker:
    policy = PolicyEngine.load()
    evidence = EvidenceRecorder("test-console-refresh", root=tmp_path, redactor=policy.redactor)
    return EscalationBroker(session, evidence, run_id="test-console-refresh", attended=True)


async def _get(broker: EscalationBroker) -> str:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_console(broker)), base_url="http://console"
    ) as client:
        return (await client.get("/")).text


class TestAutoRefreshOnlyWhileIdle:
    async def test_the_idle_page_still_refreshes(self, tmp_path):
        broker = _make_broker(tmp_path, FakeSession())
        page = await _get(broker)
        assert 'http-equiv="refresh"' in page

    async def test_a_page_with_an_open_card_does_not_refresh(self, tmp_path):
        session = FakeSession()
        broker = _make_broker(tmp_path, session)
        task = asyncio.create_task(
            broker.raise_intervention("irreversible_action", {"step": 1})
        )
        for _ in range(200):
            if broker.open_requests:
                break
            await asyncio.sleep(0.01)

        page = await _get(broker)
        assert 'http-equiv="refresh"' not in page

        broker.resolve(
            broker.open_requests[0].id,
            OperatorDecision(mode=ResumeMode.PERFORMED_MANUALLY),
        )
        await task
