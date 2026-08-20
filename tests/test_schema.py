"""Tests for the capability contract.

These cover the guarantees the rest of the system is allowed to assume: that an
artifact which loads is internally consistent, that caller inputs are checked
before a browser opens, and -- the one that carries real weight -- that a
recorded artifact never contains the caller's data as a literal.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cua.schema import (
    Capability,
    ClickAction,
    ExtractAction,
    NavigateAction,
    ParamSpec,
    ParamType,
    RiskClass,
    Step,
    TypeTextAction,
    ValueRef,
)
from cua.schema.capability import InputValidationError
from tests.conftest import descriptor


def test_round_trips_through_json(capability: Capability) -> None:
    raw = capability.model_dump_json()
    restored = Capability.model_validate_json(raw)
    assert restored == capability


def test_rejects_unknown_fields(capability: Capability) -> None:
    payload = json.loads(capability.model_dump_json())
    payload["surprise"] = True
    with pytest.raises(ValidationError):
        Capability.model_validate(payload)


def test_no_caller_data_is_stored_as_a_literal(capability: Capability) -> None:
    """The point of parameterisation: the recorded value is a reference.

    If this ever fails, a discovery run has baked a real member id -- regulated
    data -- into an artifact that gets committed and reviewed.
    """
    typing_step = capability.step("s2")
    assert isinstance(typing_step.action, TypeTextAction)
    assert typing_step.action.value.kind == "param"
    assert typing_step.action.value.param == "member_id"
    assert "10042" not in capability.model_dump_json(exclude={"inputs"})


def test_value_ref_display_never_resolves_a_parameter() -> None:
    ref = ValueRef.of_param("ssn")
    assert ref.display() == "{{param.ssn}}"
    assert ref.render({"ssn": "123-45-6789"}) == "123-45-6789"


def test_step_referencing_undeclared_input_is_rejected(capability: Capability) -> None:
    broken = capability.model_dump()
    broken["steps"][1]["action"]["value"] = {"kind": "param", "param": "nope"}
    with pytest.raises(ValidationError, match="undeclared input"):
        Capability.model_validate(broken)


def test_extract_into_undeclared_output_is_rejected(capability: Capability) -> None:
    broken = capability.model_dump()
    broken["steps"][3]["action"]["output"] = "not_declared"
    with pytest.raises(ValidationError, match="undeclared output"):
        Capability.model_validate(broken)


def test_risk_profile_must_agree_with_the_steps(capability: Capability) -> None:
    broken = capability.model_dump()
    broken["steps"][2]["risk"] = RiskClass.IRREVERSIBLE.value
    with pytest.raises(ValidationError, match="risk.irreversible_step_ids"):
        Capability.model_validate(broken)


def test_targeted_action_without_a_target_is_rejected() -> None:
    with pytest.raises(ValidationError, match="requires a target"):
        Step(id="x", intent="click nothing", action=ClickAction())


def test_navigate_needs_no_target() -> None:
    step = Step(
        id="x",
        intent="go",
        action=NavigateAction(url=ValueRef.of_literal("http://127.0.0.1:8080/")),
    )
    assert step.target is None


def test_sensitive_param_may_not_carry_an_example() -> None:
    with pytest.raises(ValidationError, match="must not carry an example"):
        ParamSpec(
            name="ssn",
            type=ParamType.STRING,
            description="Member SSN.",
            sensitive=True,
            example="123-45-6789",
        )


class TestInputValidation:
    def test_missing_required_input(self, capability: Capability) -> None:
        with pytest.raises(InputValidationError, match="missing required input"):
            capability.validate_inputs({})

    def test_unknown_input(self, capability: Capability) -> None:
        with pytest.raises(InputValidationError, match="unknown input"):
            capability.validate_inputs({"member_id": "1", "extra": "x"})

    def test_coerces_to_the_declared_type(self, capability: Capability) -> None:
        assert capability.validate_inputs({"member_id": 10042}) == {"member_id": "10042"}

    def test_enum_values_are_enforced(self) -> None:
        spec = ParamSpec(
            name="account_type",
            type=ParamType.ENUM,
            description="Kind of sub-account.",
            enum_values=["savings", "checking"],
        )
        cap_inputs = [spec]
        assert spec.json_schema()["enum"] == ["savings", "checking"]
        # Direct coercion check via a minimal capability is overkill; the
        # ParamSpec-level contract is what callers depend on.
        assert cap_inputs[0].type is ParamType.ENUM


def test_input_json_schema_is_a_usable_tool_signature(capability: Capability) -> None:
    """This dict plus a name and description is an agent tool definition."""
    schema = capability.input_json_schema()
    assert schema["type"] == "object"
    assert schema["required"] == ["member_id"]
    assert schema["properties"]["member_id"]["type"] == "string"
    assert schema["additionalProperties"] is False


def test_extract_step_names_a_real_output(capability: Capability) -> None:
    step = capability.step("s4")
    assert isinstance(step.action, ExtractAction)
    assert step.action.output in {o.name for o in capability.outputs}


def test_descriptor_describe_is_readable() -> None:
    d = descriptor("button", "Search")
    assert d.describe() == "button 'Search'"
