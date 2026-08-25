"""End-to-end test of the escalation and control-transfer path.

This is the part of the system most likely to be a plausible-looking stub, so it
is tested against the real pieces: a real browser session, the real policy gate
stopping a real irreversible action, the real console rendering the intervention,
and a real HTTP round trip taking control and resuming.

Requires the mock app (on MOCKAPP_PORT, default :8080) and a capability recorded
from it; both are set up by the fixtures. Marked ``integration`` so the fast
unit suite stays fast.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from pathlib import Path

import httpx
import pytest

from cua.config import MOCKAPP_HOST as _MOCKAPP_HOST, MOCKAPP_PORT as _MOCKAPP_PORT
from cua.escalation.broker import EscalationBroker
from cua.escalation.console import build_console
from cua.escalation.models import InterventionStatus, ResumeMode
from cua.evidence import EvidenceRecorder
from cua.policy import PolicyEngine
from cua.replay import ReplayEngine, ResumeDecision
from cua.schema.capability import Capability
from cua.session import ControlOwner, SessionManager
from cua.surfaces.web import WebSurface

pytestmark = pytest.mark.integration

# cua.config is the single source of truth for where the mock app is (see its
# own docstring for why mockapp/app.py deliberately keeps an independent copy
# of this instead of importing it) -- socket.connect_ex needs the port as an
# int, which is the one adaptation needed here.
_MOCKAPP_PORT_INT = int(_MOCKAPP_PORT)
MOCKAPP = f"http://{_MOCKAPP_HOST}:{_MOCKAPP_PORT}"
CAPABILITY = Path("capabilities/cu.member.open_subaccount.json")


def _mockapp_running() -> bool:
    with contextlib.closing(socket.socket()) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((_MOCKAPP_HOST, _MOCKAPP_PORT_INT)) == 0


needs_app = pytest.mark.skipif(
    not _mockapp_running(), reason=f"the mock app is not running on :{_MOCKAPP_PORT}"
)
needs_capability = pytest.mark.skipif(
    not CAPABILITY.exists(), reason="record cu.member.open_subaccount first"
)


@needs_app
@needs_capability
async def test_operator_takes_the_live_session_and_hands_it_back(tmp_path):
    """The full loop: stop on an irreversible action, hand over, resume, finish.

    The assertion that matters is not just that the run completes -- it is that
    control genuinely moves, that the session the operator is handed is the one
    the automation was parked on, and that automation is forbidden from acting
    while the human holds it.
    """
    capability = Capability.model_validate_json(CAPABILITY.read_text(encoding="utf-8"))
    assert capability.approval == "draft"
    assert capability.risk.contains_irreversible, "this test needs a risky step to stop on"
    # The committed fixture's entry_point is baked in from whatever port it was
    # recorded against. Overriding it here, the same way cli.py's --entry does,
    # means this test targets wherever mockapp is actually running right now
    # rather than requiring every capability be re-recorded after a port change.
    capability = capability.model_copy(
        update={"surface": capability.surface.model_copy(update={"entry_point": MOCKAPP + "/"})}
    )

    policy = PolicyEngine.load()
    evidence = EvidenceRecorder("test-escalation", root=tmp_path, redactor=policy.redactor)
    surface = await WebSurface.launch(headless=True)
    session = SessionManager(surface=surface, evidence=evidence)
    broker = EscalationBroker(
        session,
        evidence,
        run_id="test-escalation",
        attended=True,
        # Short, so an intervention nobody answers surfaces as a fast
        # failure instead of the production-length operator wait.
        timeout_seconds=20,
    )

    # An async client rather than fastapi's TestClient: TestClient is synchronous
    # and would block the very event loop the paused replay task needs in order
    # to make progress, so the test would deadlock against itself.
    console = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_console(broker)),
        base_url="http://console",
    )

    observed_owner_during_handover: list[ControlOwner] = []

    async def operator() -> None:
        """Stand in for a person working the console."""
        # Eight steps of a real browser flow run before the escalation, each
        # waiting on a real navigation, so this is a generous window rather than
        # a tight one -- the assertion is that it happens, not that it is quick.
        for _ in range(1200):
            if broker.open_requests:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover
            pytest.fail("no intervention was raised within 60s")

        request = broker.open_requests[0]

        # The console renders the request with enough context to act on.
        page = (await console.get("/")).text
        assert request.reason in page
        assert "Take control of the live session" in page

        # No auto-refresh while a card is showing -- a scheduled reload
        # racing the operator's own click on "Take control" is what silently
        # dropped the request client-side before it ever reached the server.
        assert "http-equiv=\"refresh\"" not in page

        # The screenshot on the request right now is the one taken when the
        # escalation was raised -- before the operator has done anything.
        screenshot_before_control = request.screenshot
        assert screenshot_before_control

        # Take control over HTTP, exactly as the operator would.
        await console.post(f"/take/{request.id}")
        assert session.owner is ControlOwner.HUMAN
        observed_owner_during_handover.append(session.owner)

        # A fresh snapshot is what the operator's page should show now, not
        # the pre-handoff frame from before they clicked anything -- that
        # frame never changing again was the actual cause of "I click Take
        # control and nothing visually happens."
        assert request.screenshot != screenshot_before_control
        assert Path(request.screenshot).exists()

        # A second /take on the same request -- a double-click, or a browser
        # resubmitting the POST on a reload -- must not crash. It used to:
        # the control FSM only allows BLOCKED -> HUMAN once, so calling
        # hand_to_human() again raised ControlViolation straight out to an
        # unhandled 500 a real operator actually hit.
        duplicate_take = await console.post(f"/take/{request.id}")
        assert duplicate_take.status_code < 500
        assert session.owner is ControlOwner.HUMAN

        # While the human holds it, automation is not allowed to act.
        with pytest.raises(Exception):
            session.control.assert_automation("click")

        # The operator does the work themselves in the live session -- the same
        # page the automation was parked on, not a fresh one. The form lives in
        # the content frame, as everything in this application does.
        assert MOCKAPP in surface.page.url
        content = surface.page.frame(name="contentframe")
        assert content is not None, "the operator was handed a session without the app in it"
        await content.click("input[type=submit]", timeout=15000)

        # A real operator watches the action land before telling the run to
        # carry on. Resuming while the page is still navigating hands the
        # automation a half-rendered screen, which it correctly refuses to act
        # on -- and then escalates again, to nobody.
        await surface.page.wait_for_load_state("networkidle", timeout=20000)

        await console.post(
            f"/resolve/{request.id}", data={"mode": ResumeMode.PERFORMED_MANUALLY.value}
        )

    async def escalate(reason: str, context: dict) -> ResumeDecision:
        decision = await broker.raise_intervention(reason, context)
        return ResumeDecision(
            mode="continue" if decision.mode is not ResumeMode.ABORT else "abort",
            note=decision.note,
            human_actions=int(decision.extra.get("human_actions", 0)) or 1,
        )

    try:
        engine = ReplayEngine(
            surface=surface, policy=policy, evidence=evidence, on_escalation=escalate
        )
        task = asyncio.create_task(
            engine.run(
                capability,
                {
                    "member_id": "10077",
                    "account_type": "Savings",
                    "nickname": "Test Account",
                    "initial_deposit": "300",
                },
            )
        )
        await operator()
        result = await asyncio.wait_for(task, timeout=180)
    finally:
        await console.aclose()
        await surface.close()

    assert observed_owner_during_handover == [ControlOwner.HUMAN]
    # Control came back, and it came back to automation rather than being stranded.
    assert session.owner is ControlOwner.AUTOMATION

    request = list(broker.requests.values())[0]
    assert request.status is InterventionStatus.RESOLVED
    assert request.resolution is ResumeMode.PERFORMED_MANUALLY

    # The handoff is in the evidence trail, not only in memory.
    log = (Path(tmp_path) / "test-escalation" / "run.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line)["event"] for line in log.splitlines() if line.strip()]
    assert "intervention.raised" in events
    assert "control.granted" in events
    assert "control.returned" in events
    assert "intervention.resolved" in events

    assert result.status.value in {"success", "failure"}
    assert result.provider_calls == 0, "replay must never consult a model"

    # _escalations used to be declared and read but never appended to --
    # result.escalations was silently always empty regardless of how many
    # escalations a run actually raised.
    assert len(result.escalations) == 1
    escalation = result.escalations[0]
    assert escalation.reason == "irreversible_action"
    assert escalation.resume_mode == "continue"
    assert escalation.resolved_at is not None


@needs_app
@needs_capability
async def test_unattended_run_records_the_intervention_and_refuses(tmp_path):
    """With nobody watching, the run does not commit and does not hang."""
    capability = Capability.model_validate_json(CAPABILITY.read_text(encoding="utf-8"))
    capability = capability.model_copy(
        update={"surface": capability.surface.model_copy(update={"entry_point": MOCKAPP + "/"})}
    )
    policy = PolicyEngine.load()
    evidence = EvidenceRecorder("test-unattended", root=tmp_path, redactor=policy.redactor)
    surface = await WebSurface.launch(headless=True)
    session = SessionManager(surface=surface, evidence=evidence)
    broker = EscalationBroker(session, evidence, run_id="test-unattended", attended=False)

    async def escalate(reason: str, context: dict) -> ResumeDecision:
        decision = await broker.raise_intervention(reason, context)
        return ResumeDecision(mode="abort", note=decision.note)

    try:
        engine = ReplayEngine(
            surface=surface, policy=policy, evidence=evidence, on_escalation=escalate
        )
        result = await asyncio.wait_for(
            engine.run(
                capability,
                {
                    "member_id": "10077",
                    "account_type": "Savings",
                    "nickname": "Test Account",
                    "initial_deposit": "300",
                },
            ),
            timeout=90,
        )
    finally:
        await surface.close()

    assert result.status.value == "failure"
    assert result.failure is not None
    assert result.failure.category.value == "human_aborted"
    # The intervention still exists -- it is what an operator queue would be fed
    # from. What is absent is the waiting, not the record.
    assert broker.requests, "an unattended run must still record the intervention"
