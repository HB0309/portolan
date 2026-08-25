"""Tests for the recorder.

The recorder is the hinge: it turns one expensive model-driven run into
something that executes deterministically forever after. The bugs it can produce
are the expensive kind -- a capability that looks parameterised, passes review,
and works for exactly the record it was recorded against.

Both of the failures guarded here were real, and both were found by looking at
the artifact rather than by a test. They have tests now.
"""

from __future__ import annotations

import pytest

from cua.discovery.orchestrator import DiscoveryStep, DiscoveryTrace
from cua.discovery.recorder import CapabilityRecorder, RecorderError, _distinguishing_text
from cua.schema.capability import ExtractAction, TypeTextAction
from cua.surfaces.base import Element, Observation, Rect


def element(role, name="", hint="", section="", row="", frame=("contentframe",)):
    return Element(
        ref="e1", role=role, name=name, label_hint=hint, section=section,
        row_text=row, frame_path=frame,
    )


def observation(*elements, url="http://127.0.0.1:8080/"):
    return Observation(url=url, title="", elements=list(elements))


@pytest.fixture
def recorder() -> CapabilityRecorder:
    return CapabilityRecorder("legacy-cu")


class TestDescriptors:
    def test_a_data_cell_is_identified_by_its_label_not_its_value(self, recorder):
        """The bug that made a capability work for exactly one member.

        A cell's accessible name is its own text -- the value. Recording that
        produces `cell "$4,102.55"`, which resolves for one record and silently
        fails for every other.
        """
        cell = element("cell", name="$4,102.55", hint="Current Savings Balance",
                       section="Balance Summary")
        descriptor = recorder.describe_element(cell)

        assert descriptor.accessible_name == "Current Savings Balance"
        assert descriptor.name_source == "adjacent_label"
        assert "$4,102.55" not in descriptor.model_dump_json()

    def test_a_named_control_is_identified_by_its_own_name(self, recorder):
        button = element("button", name="Search", section="Member Search")
        descriptor = recorder.describe_element(button)
        assert descriptor.accessible_name == "Search"
        assert descriptor.name_source == "accessible_name"

    def test_an_unnamed_field_falls_back_to_its_adjacent_label(self, recorder):
        field = element("textbox", name="", hint="Member Number", section="Member Search")
        descriptor = recorder.describe_element(field)
        assert descriptor.accessible_name == "Member Number"
        assert descriptor.name_source == "adjacent_label"
        assert descriptor.recorded_confidence < 1.0

    def test_anchors_capture_frame_and_panel(self, recorder):
        descriptor = recorder.describe_element(
            element("button", name="Search", section="Member Search")
        )
        kinds = {a.kind.value: a.value for a in descriptor.anchors}
        assert kinds["frame"] == "contentframe"
        assert kinds["heading"] == "Member Search"

    def test_every_descriptor_carries_a_robustness_note(self, recorder):
        for el in (
            element("button", name="Search"),
            element("textbox", hint="Member Number"),
            element("cell", name="$1.00", hint="Balance"),
        ):
            assert len(recorder.describe_element(el).robustness_note) > 40


class TestValueParameterisation:
    def test_a_typed_input_becomes_a_parameter_reference(self, recorder):
        ref = recorder._value_ref("10042", {"member_id": "10042"})
        assert ref.kind == "param"
        assert ref.param == "member_id"

    def test_a_url_embedding_an_input_becomes_a_template(self, recorder):
        """Canonicalising the route is what makes a deep link reusable."""
        ref = recorder._value_ref(
            "http://127.0.0.1:8080/member?id=10042", {"member_id": "10042"}
        )
        assert ref.kind == "template"
        assert "{{param.member_id}}" in ref.template
        assert "10042" not in ref.template

    def test_a_genuine_constant_stays_literal(self, recorder):
        ref = recorder._value_ref("Savings", {"member_id": "10042"})
        assert ref.kind == "literal"
        assert ref.literal == "Savings"


class TestIntentScrubbing:
    """A real model restates the value it is working with when explaining
    itself -- the scripted stand-in used elsewhere in these tests never does,
    because its canned explanations were written with no real value to
    restate. Found by running a real model against Groq: it wrote "Click
    Search to retrieve member 10042's details" as its stated reason, and the
    recorder's own safety sweep correctly refused to emit a capability with
    that literal in it -- discarding an otherwise-valid run. Scrubbing the
    intent the same way step values are scrubbed is what saves it.

    The same substitution -- ``_scrub_text`` -- turned out to be needed in a
    second place too: see ``TestFallbackTextScrubbing`` below.
    """

    def test_a_caller_value_in_the_models_own_words_is_parameterized(self, recorder):
        text = recorder._scrub_text(
            "Click Search to retrieve member 10042's details",
            {"member_id": "10042"},
        )
        assert "10042" not in text
        assert "{{param.member_id}}" in text

    def test_intent_with_no_caller_value_is_unchanged(self, recorder):
        text = recorder._scrub_text(
            "Sign in to reach the member search screen", {"member_id": "10042"}
        )
        assert text == "Sign in to reach the member search screen"

    def test_a_scrubbed_intent_survives_the_final_safety_sweep(self, recorder):
        """The end-to-end case: the run that used to be discarded now records."""
        trace = DiscoveryTrace(
            goal="read a balance",
            entry_point="http://127.0.0.1:8080/",
            inputs={"member_id": "10042"},
            outputs=["savings_balance"],
        )
        after = observation(element("cell", name="x", section="Member Detail"))
        trace.steps = [
            DiscoveryStep(
                index=1, tool="click",
                arguments={}, intent="Click Search to retrieve member 10042's details",
                element=element("button", name="Search", section="Search By"),
                observation_before=after, observation_after=after, outcome="clicked",
            ),
        ]
        capability = recorder.record(
            trace, capability_id="cu.test", name="t", description="d",
            run_id="r", provider="groq", model="openai/gpt-oss-120b",
        )
        assert "10042" not in capability.step("s1").intent
        assert "{{param.member_id}}" in capability.step("s1").intent


class TestFallbackTextScrubbing:
    """Found live, driven by a human through the operator console: the model
    retried an ``extract`` call three times hunting for a reference number and,
    on one of those tries, targeted the "Initial Deposit" cell instead. The
    *targeted* cell was labeled correctly -- that part of the descriptor was
    never the bug -- but ``describe_element`` also folds the whole containing
    table row into a ``FallbackHint`` for resilience against layout drift, and
    that row held more than one field ("Initial Deposit $500"). The caller's
    ``initial_deposit`` value rode along in the fallback text even though the
    step never meant to touch that value, and the safety sweep correctly
    refused to emit the capability. A deterministic replay of the same flow
    with the scripted provider never mis-targets a cell, so this had no test
    until a real model produced it.
    """

    def test_a_caller_value_in_a_fallback_row_is_parameterized(self, recorder):
        el = element(
            "cell", name="SA-423213", section="Transaction Detail",
            row="Initial Deposit $500",
        )
        descriptor = recorder.describe_element(el, {"initial_deposit": "500"})
        fallback_values = [f.value for f in descriptor.fallbacks]
        assert not any("500" in v for v in fallback_values)
        assert any("{{param.initial_deposit}}" in v for v in fallback_values)

    def test_a_caller_value_quoted_from_a_label_hint_is_parameterized(self, recorder):
        """The robustness note quotes the label a cell was matched on, and a
        label can itself contain the value being read (e.g. an amount column
        whose header restates a figure). Contrived here to exercise the note's
        own scrub rather than rely on reproducing the exact live label text.
        """
        el = element("cell", name="", hint="Amount 500", section="Transaction Detail")
        descriptor = recorder.describe_element(el, {"initial_deposit": "500"})
        assert "500" not in descriptor.robustness_note
        assert "{{param.initial_deposit}}" in descriptor.robustness_note


class TestBBoxCoordinatesAreNotCallerData:
    """Found live, a second time, over the same input: a capability was
    refused for 'initial_deposit' = "500" that was not a leak at all. An
    unrelated field's positional FallbackHint had ``y: 500.0`` -- a
    completely ordinary pixel coordinate -- and the safety sweep used to
    substring-match against the step's raw serialized JSON, where a float
    500.0 renders as the text "500.0" and trips the same check a real leak
    would. Caller data can only ever reach a step as a string (every value
    that flows through _value_ref or _scrub_text ends up as text, never a
    bare number), so a short numeric input coincidentally matching some
    element's x/y/width/height is not a rare accident -- it is close to
    certain over enough steps on a real screen.
    """

    def test_a_coincidental_bbox_coordinate_does_not_trip_the_guard(self, recorder):
        el = Element(
            ref="e1", role="textbox", name="", label_hint="Nickname",
            section="New Sub-Account", frame_path=("contentframe",),
            rect=Rect(x=120.0, y=500.0, width=180.0, height=22.0),
        )
        descriptor = recorder.describe_element(el, {"initial_deposit": "500"})
        assert any(f.kind.value == "bbox" and f.bbox.y == 500.0 for f in descriptor.fallbacks)

        trace = DiscoveryTrace(
            goal="open a sub-account",
            entry_point="http://127.0.0.1:8080/",
            inputs={"initial_deposit": "500"},
            outputs=[],
        )
        after = observation(el)
        trace.steps = [
            DiscoveryStep(
                index=1, tool="click", arguments={}, intent="Focus the nickname field",
                element=el, observation_before=after, observation_after=after,
                outcome="clicked",
            ),
        ]
        # Must not raise: nothing about this step actually contains the
        # caller's deposit amount as text, only as an unrelated coordinate.
        capability = recorder.record(
            trace, capability_id="cu.test", name="t", description="d",
            run_id="r", provider="scripted", model="scripted",
        )
        assert capability.step("s1").target is not None

    def test_a_real_leak_in_a_string_field_is_still_caught(self, recorder):
        """The regression guard for the guard: narrowing the sweep to string
        leaves must not stop catching an actual leak. A cell with no
        label_hint is identified by its own text (the D13 shape) -- the one
        descriptor field that is never scrubbed, since scrubbing the
        accessible name used for matching would break targeting rather than
        protect data. That field is exactly where a real leak must still be
        caught.
        """
        el = element("cell", name="500", section="Transaction Detail")
        trace = DiscoveryTrace(
            goal="open a sub-account",
            entry_point="http://127.0.0.1:8080/",
            inputs={"initial_deposit": "500"},
            outputs=[],
        )
        after = observation(el)
        trace.steps = [
            DiscoveryStep(
                index=1, tool="click", arguments={}, intent="Read the deposit amount",
                element=el, observation_before=after, observation_after=after,
                outcome="clicked",
            ),
        ]
        with pytest.raises(RecorderError):
            recorder.record(
                trace, capability_id="cu.test", name="t", description="d",
                run_id="r", provider="scripted", model="scripted",
            )


class TestCheckpointMarkers:
    def test_a_marker_never_contains_caller_data(self):
        """The other bug: a checkpoint only one input could ever satisfy."""
        obs = observation(
            element("cell", name="10042", section="Member Detail"),
        )
        marker, _ = _distinguishing_text(obs, forbidden={"10042"})
        assert "10042" not in marker

    def test_a_marker_is_not_a_value(self):
        obs = observation(element("cell", name="$4,102.55", section="$4,102.55"))
        marker, _ = _distinguishing_text(obs, forbidden=set())
        assert marker == ""

    def test_navigation_chrome_is_never_used_as_a_marker(self):
        """Chrome is on every page, so a marker from it passes everywhere."""
        obs = observation(
            element("link", name="Member Detail", section="Servicing", frame=("navframe",)),
            element("cell", name="x", section="Balance Summary"),
        )
        marker, frame = _distinguishing_text(obs, forbidden=set())
        assert marker == "Balance Summary"
        assert frame == "contentframe"

    def test_the_marker_reports_the_frame_it_was_found_in(self):
        obs = observation(element("cell", name="x", section="Member Detail"))
        _, frame = _distinguishing_text(obs, forbidden=set())
        assert frame == "contentframe"


class TestRecordedArtifact:
    def _trace(self) -> DiscoveryTrace:
        before = observation(element("textbox", hint="Member Number", section="Member Search"))
        after = observation(element("cell", name="x", section="Member Detail"))
        trace = DiscoveryTrace(
            goal="read a balance",
            entry_point="http://127.0.0.1:8080/",
            inputs={"member_id": "10042"},
            outputs=["savings_balance"],
        )
        trace.steps = [
            DiscoveryStep(
                index=1, tool="type_text",
                arguments={"text": "10042"}, intent="enter the member number",
                element=element("textbox", hint="Member Number", section="Member Search"),
                observation_before=before, observation_after=after, outcome="typed",
            ),
            DiscoveryStep(
                index=2, tool="extract",
                arguments={"output": "savings_balance"}, intent="read the balance",
                element=element("cell", name="$4,102.55", hint="Current Savings Balance",
                                section="Balance Summary"),
                observation_before=after, observation_after=after, outcome="read",
                extracted="$4,102.55", output_name="savings_balance",
            ),
        ]
        trace.collected = {"savings_balance": "$4,102.55"}
        return trace

    def test_produces_a_valid_capability(self, recorder):
        capability = recorder.record(
            self._trace(), capability_id="cu.test", name="t", description="d",
            run_id="r", provider="scripted", model="m",
        )
        assert capability.capability_id == "cu.test"
        assert [s.id for s in capability.steps] == ["s1", "s2"]
        assert isinstance(capability.step("s1").action, TypeTextAction)
        assert isinstance(capability.step("s2").action, ExtractAction)

    def test_the_caller_value_never_appears_in_a_step(self, recorder):
        capability = recorder.record(
            self._trace(), capability_id="cu.test", name="t", description="d",
            run_id="r", provider="scripted", model="m",
        )
        steps_json = "".join(s.model_dump_json() for s in capability.steps)
        assert "10042" not in steps_json
        assert "{{param.member_id}}" in capability.step("s1").action.value.display()

    def test_product_outcomes_and_recovery_are_attached(self, recorder):
        capability = recorder.record(
            self._trace(), capability_id="cu.test", name="t", description="d",
            run_id="r", provider="scripted", model="m",
        )
        assert "MEMBER_NOT_FOUND" in {o.code for o in capability.outcomes}
        assert "dismiss_system_notice" in {r.id for r in capability.recovery}

    def test_declares_the_extracted_output(self, recorder):
        capability = recorder.record(
            self._trace(), capability_id="cu.test", name="t", description="d",
            run_id="r", provider="scripted", model="m",
        )
        assert [o.name for o in capability.outputs] == ["savings_balance"]
        assert capability.outputs[0].produced_by_step == "s2"

    def test_a_recorded_capability_starts_as_a_draft(self, recorder):
        capability = recorder.record(
            self._trace(), capability_id="cu.test", name="t", description="d",
            run_id="r", provider="scripted", model="m",
        )
        assert capability.approval == "draft"

    def test_an_empty_run_records_nothing(self, recorder):
        empty = DiscoveryTrace(goal="g", entry_point="e", inputs={}, outputs=[])
        with pytest.raises(RecorderError, match="nothing to record"):
            recorder.record(
                empty, capability_id="cu.test", name="t", description="d",
                run_id="r", provider="scripted", model="m",
            )
