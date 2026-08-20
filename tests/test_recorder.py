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
from cua.surfaces.base import Element, Observation


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
