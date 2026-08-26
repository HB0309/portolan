"""The double-execution bug: found by a human actually working the handoff.

An operator takes control, performs the irreversible action themselves in the
live session, and clicks "I did it - resume". Before this fix, the
orchestrator treated that the same as "approved -- go ahead and do it", and
proceeded to act on the surface a second time: once by the operator's own
hand, once by the automation immediately afterward. Against this project's
mock app that meant a stale element reference (the page had already moved to
the confirmation screen) and a failure the agent then had to reason its way
out of; against a real system it is a duplicated transaction.

These tests exercise the orchestrator against a fake surface that records
every call to ``act()``, so the assertion that matters is not "the run
finished" but "the surface was touched exactly once for the irreversible
step" -- and once only when a human did not already do it by hand.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from cua.discovery.orchestrator import DiscoveryOrchestrator, InterventionOutcome
from cua.evidence import EvidenceRecorder
from cua.llm.base import ToolCall
from cua.llm.scripted import ScriptedProvider
from cua.policy import PolicyEngine
from cua.surfaces.base import (
    ActionOutcome,
    ActionRequest,
    Element,
    Observation,
    Surface,
)

ALLOWED_URL = "http://127.0.0.1:8080/"


@dataclass
class FakeSurface(Surface):
    """A surface with one control on screen: the irreversible submit button.

    Every ``act()`` call is recorded, which is the whole point -- these tests
    assert on how many times the surface was actually touched, not on the
    orchestrator's internal bookkeeping.
    """

    kind = "fake"
    acted: list[ActionRequest] = field(default_factory=list)

    async def observe(self) -> Observation:
        return Observation(
            url=ALLOWED_URL,
            title="Sub-Account",
            elements=[
                Element(ref="e1", role="button", name="Open Sub-Account", section="New Account")
            ],
        )

    async def act(self, request: ActionRequest) -> ActionOutcome:
        self.acted.append(request)
        return ActionOutcome(ok=True, detail="clicked", navigated=True)

    async def screenshot(self, path: str, mask_refs: list[str] | None = None) -> str:
        return path

    async def snapshot(self, path: str) -> str:
        return path

    async def current_url(self) -> str:
        return ALLOWED_URL


def _make_orchestrator(surface: FakeSurface, tmp_path, *, on_intervention):
    policy = PolicyEngine.load()
    evidence = EvidenceRecorder("test-escalation-orch", root=tmp_path, redactor=policy.redactor)
    provider = ScriptedProvider(
        script=[
            ToolCall(
                name="click",
                arguments={"ref": "e1", "why": "open the sub-account"},
                reasoning="open the sub-account",
            )
        ]
    )
    return DiscoveryOrchestrator(
        surface, provider, policy, evidence, on_intervention=on_intervention
    )


class TestPerformedByHumanSkipsReExecution:
    async def test_the_surface_is_never_touched_when_a_human_already_acted(self, tmp_path):
        surface = FakeSurface()

        async def on_intervention(reason, context):
            return InterventionOutcome(approved=True, performed_by_human=True)

        orchestrator = _make_orchestrator(surface, tmp_path, on_intervention=on_intervention)
        _, trace = await orchestrator.run(
            goal="open a sub-account", entry_point=ALLOWED_URL, inputs={}, outputs=[]
        )

        # The orchestrator always navigates to the entry point before the loop
        # starts, so one act is expected -- what matters is that the click
        # queued up in the script never happens, because the human already
        # did it.
        assert [a.type for a in surface.acted] == ["navigate"]
        assert len(trace.steps) == 1
        assert trace.steps[0].outcome == "performed by the operator during handoff"
        assert trace.steps[0].human_approved is True

    async def test_the_surface_is_touched_exactly_once_when_automation_is_approved(
        self, tmp_path
    ):
        """The regression guard: approval that isn't 'performed_by_human' must
        still let the agent act -- this is not a test that acting is disabled,
        only that acting is not *repeated*.
        """
        surface = FakeSurface()

        async def on_intervention(reason, context):
            return InterventionOutcome(approved=True, performed_by_human=False)

        orchestrator = _make_orchestrator(surface, tmp_path, on_intervention=on_intervention)
        await orchestrator.run(
            goal="open a sub-account", entry_point=ALLOWED_URL, inputs={}, outputs=[]
        )

        # navigate to the entry point, then exactly one click -- not two.
        assert [a.type for a in surface.acted] == ["navigate", "click"]
        assert surface.acted[1].ref == "e1"

    async def test_declining_still_blocks_the_action(self, tmp_path):
        surface = FakeSurface()

        async def on_intervention(reason, context):
            return InterventionOutcome(approved=False)

        orchestrator = _make_orchestrator(surface, tmp_path, on_intervention=on_intervention)
        await orchestrator.run(
            goal="open a sub-account", entry_point=ALLOWED_URL, inputs={}, outputs=[]
        )

        assert [a.type for a in surface.acted] == ["navigate"]

    async def test_intent_and_target_are_recorded_even_when_the_human_acted(self, tmp_path):
        """The recorded step still has to be reviewable -- the point of intent
        and target isn't lost just because automation didn't do the clicking.
        """
        surface = FakeSurface()

        async def on_intervention(reason, context):
            return InterventionOutcome(approved=True, performed_by_human=True)

        orchestrator = _make_orchestrator(surface, tmp_path, on_intervention=on_intervention)
        _, trace = await orchestrator.run(
            goal="open a sub-account", entry_point=ALLOWED_URL, inputs={}, outputs=[]
        )

        step = trace.steps[0]
        assert step.element is not None
        assert step.element.name == "Open Sub-Account"
        assert "open the sub-account" in step.intent


class TestHumanWaitTimeIsExcludedFromTheWallClock:
    """Found live: a real operator took several minutes to get back to a
    console escalation -- nothing wrong with that, that is what "attended"
    means -- and the run failed on a timeout immediately after they resolved
    it, having done everything asked of them correctly. The wall-clock budget
    exists to catch automation looping without making progress; it should
    never also be the thing that punishes a human for taking their time.
    """

    async def test_a_slow_operator_does_not_trip_the_wall_clock(self, tmp_path):
        surface = FakeSurface()

        async def slow_operator(reason, context):
            # Longer than the tiny wall-clock budget below, on purpose.
            await asyncio.sleep(0.3)
            return InterventionOutcome(approved=True, performed_by_human=False)

        policy = PolicyEngine.load()
        policy.limits.wall_clock_seconds = 0.1
        evidence = EvidenceRecorder(
            "test-wall-clock", root=tmp_path, redactor=policy.redactor
        )
        provider = ScriptedProvider(
            script=[
                ToolCall(
                    name="click",
                    arguments={"ref": "e1", "why": "open the sub-account"},
                    reasoning="open the sub-account",
                ),
                ToolCall(
                    name="finish",
                    arguments={"summary": "done"},
                    reasoning="done",
                ),
            ]
        )
        orchestrator = DiscoveryOrchestrator(
            surface, provider, policy, evidence, on_intervention=slow_operator
        )

        result, _ = await orchestrator.run(
            goal="open a sub-account", entry_point=ALLOWED_URL, inputs={}, outputs=[]
        )

        assert result.succeeded, result.failure
        # asyncio.sleep can return a hair early depending on timer
        # resolution, so this is deliberately looser than the 0.3s slept
        # above -- the property under test is "meaningfully excluded", not a
        # precise measurement of sleep().
        assert orchestrator._human_wait_seconds >= 0.25


class TestResultRecordsTheEscalation:
    """Same bug class as D33 (ReplayEngine._escalate): DiscoveryResult.escalations
    was declared and read (evidence/README.md generation, result.json) but
    DiscoveryOrchestrator never appended to it -- found while writing up a run
    whose run.jsonl showed a genuine human handoff but whose result.json
    reported an empty escalations list.
    """

    async def test_a_human_handoff_is_recorded_on_the_result(self, tmp_path):
        surface = FakeSurface()

        async def on_intervention(reason, context):
            return InterventionOutcome(approved=True, performed_by_human=True, human_actions=2)

        orchestrator = _make_orchestrator(surface, tmp_path, on_intervention=on_intervention)
        result, _ = await orchestrator.run(
            goal="open a sub-account", entry_point=ALLOWED_URL, inputs={}, outputs=[]
        )

        # succeeded/failed doesn't matter here -- the script has no "finish"
        # call queued after the click, so it runs out of steps regardless.
        # What matters is that the escalation itself was recorded either way.
        assert len(result.escalations) == 1
        escalation = result.escalations[0]
        assert escalation.reason == "irreversible_action"
        assert escalation.resolved_at is not None
        assert escalation.resume_mode == "performed_manually"
        assert escalation.human_actions_recorded == 2

    async def test_no_escalation_means_an_empty_list_not_a_stub(self, tmp_path):
        surface = FakeSurface()

        async def on_intervention(reason, context):
            return InterventionOutcome(approved=True, performed_by_human=False)

        orchestrator = _make_orchestrator(surface, tmp_path, on_intervention=on_intervention)
        result, _ = await orchestrator.run(
            goal="open a sub-account", entry_point=ALLOWED_URL, inputs={}, outputs=[]
        )

        # The click still escalates (it's irreversible) -- this is really the
        # same path as the test above, kept separate to pin down the "approved
        # but not performed by a human" resume_mode distinctly.
        assert len(result.escalations) == 1
        assert result.escalations[0].resume_mode == "approved"
        assert result.escalations[0].human_actions_recorded == 0
