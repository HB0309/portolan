"""Tests for tenant overlays.

An overlay is how one recording serves many institutions running the same vendor
product. The behaviour worth guarding is that it stays *narrow and reviewable*:
it patches what it names, it never mutates the base, and it refuses to apply in
the situations where applying it quietly would be worse than failing.
"""

from __future__ import annotations

import pytest

from cua.schema.capability import Capability
from cua.schema.overlay import (
    DescriptorOverride,
    OverlayMismatch,
    StepDisable,
    TenantOverlay,
    resolve,
)


def overlay(**kwargs) -> TenantOverlay:
    kwargs.setdefault("tenant_id", "cascade")
    kwargs.setdefault("capability_id", "cu.member.read_savings_balance")
    kwargs.setdefault("base_version", 1)
    return TenantOverlay(**kwargs)


def test_no_overlay_returns_the_capability_unchanged(capability):
    assert resolve(capability, None) is capability


def test_overrides_only_the_named_field(capability):
    result = resolve(
        capability,
        overlay(
            descriptor_overrides=[
                DescriptorOverride(
                    step_id="s3", accessible_name="Find Member", reason="renamed by tenant"
                )
            ]
        ),
    )
    target = result.step("s3").target
    assert target.accessible_name == "Find Member"
    # Everything else about the targeting is inherited, which is what keeps an
    # overlay small enough to review.
    assert target.role == capability.step("s3").target.role
    assert target.robustness_note == capability.step("s3").target.robustness_note


def test_the_base_capability_is_never_mutated(capability):
    original = capability.step("s3").target.accessible_name
    resolve(
        capability,
        overlay(
            descriptor_overrides=[
                DescriptorOverride(step_id="s3", accessible_name="Find Member", reason="x")
            ]
        ),
    )
    assert capability.step("s3").target.accessible_name == original


def test_entry_point_override_applies(capability):
    result = resolve(capability, overlay(entry_point="http://127.0.0.1:8080/?tenant=beta"))
    assert result.surface.entry_point.endswith("tenant=beta")
    assert result.surface.product == capability.surface.product


def test_refuses_a_capability_it_was_not_written_for(capability):
    with pytest.raises(OverlayMismatch, match="overlay targets"):
        resolve(capability, overlay(capability_id="something.else"))


def test_refuses_to_apply_across_a_re_recording(capability):
    """A bumped base version means the flow changed underneath the overlay.

    Applying anyway would silently pair a tenant's patch with a recording it was
    never reviewed against -- the exact failure this field exists to prevent.
    """
    bumped = capability.model_copy(update={"version": 2})
    with pytest.raises(OverlayMismatch, match="re-review"):
        resolve(bumped, overlay(base_version=1))


def test_refuses_to_reference_a_step_that_does_not_exist(capability):
    with pytest.raises(OverlayMismatch, match="unknown step"):
        resolve(
            capability,
            overlay(
                descriptor_overrides=[
                    DescriptorOverride(step_id="s99", accessible_name="x", reason="y")
                ]
            ),
        )


def test_disabling_a_step_removes_it(capability):
    result = resolve(
        capability,
        overlay(disabled_steps=[StepDisable(step_id="s1", reason="tenant skips the splash")]),
    )
    assert [s.id for s in result.steps] == ["s2", "s3", "s4"]


def test_refuses_to_disable_a_step_that_produces_a_declared_output(capability):
    """Otherwise the capability advertises a return value it can no longer deliver."""
    with pytest.raises(OverlayMismatch, match="declared output"):
        resolve(
            capability,
            overlay(disabled_steps=[StepDisable(step_id="s4", reason="oops")]),
        )


def test_refuses_two_overrides_for_the_same_step():
    with pytest.raises(ValueError, match="more than one descriptor override"):
        overlay(
            descriptor_overrides=[
                DescriptorOverride(step_id="s3", accessible_name="a", reason="x"),
                DescriptorOverride(step_id="s3", accessible_name="b", reason="y"),
            ]
        )


def test_outcome_overrides_replace_by_code(capability):
    from cua.schema.capability import BusinessOutcome, Predicate, PredicateKind

    replacement = BusinessOutcome(
        code="MEMBER_NOT_FOUND",
        description="This tenant words it differently.",
        detector=Predicate(kind=PredicateKind.TEXT_PRESENT, text="No such member"),
    )
    result = resolve(capability, overlay(outcome_overrides=[replacement]))
    codes = [o.code for o in result.outcomes]
    assert codes.count("MEMBER_NOT_FOUND") == 1
    assert result.outcomes[0].detector.text == "No such member"
