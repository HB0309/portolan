"""Shared fixtures. Kept small on purpose -- the tests that matter here are the
ones covering the load-bearing contracts, not a broad unit-test sweep.
"""

from __future__ import annotations

import pytest

from cua.schema import (
    BusinessOutcome,
    Capability,
    Checkpoint,
    ClickAction,
    ElementDescriptor,
    ExtractAction,
    NavigateAction,
    OutputSpec,
    ParamSpec,
    ParamType,
    Predicate,
    PredicateKind,
    Provenance,
    Step,
    SurfaceBinding,
    TypeTextAction,
    ValueRef,
)


def descriptor(role: str, name: str, note: str = "vendor-supplied caption") -> ElementDescriptor:
    return ElementDescriptor(role=role, accessible_name=name, robustness_note=note)


@pytest.fixture
def capability() -> Capability:
    """A small but structurally complete capability, shaped like a real one."""
    return Capability(
        capability_id="cu.member.read_savings_balance",
        name="Read member savings balance",
        description="Look up a member by id and return their current savings balance.",
        surface=SurfaceBinding(
            entry_point="http://127.0.0.1:8080/",
            product="legacy-cu",
            product_version="4.2",
        ),
        inputs=[
            ParamSpec(
                name="member_id",
                type=ParamType.STRING,
                description="The member's account number.",
                example="10042",
            )
        ],
        outputs=[
            OutputSpec(
                name="savings_balance",
                type=ParamType.STRING,
                description="Current savings balance, as displayed.",
                produced_by_step="s4",
            )
        ],
        steps=[
            Step(
                id="s1",
                intent="Open the servicing console.",
                action=NavigateAction(url=ValueRef.of_literal("http://127.0.0.1:8080/search")),
            ),
            Step(
                id="s2",
                intent="Enter the member id the caller supplied.",
                action=TypeTextAction(value=ValueRef.of_param("member_id")),
                target=descriptor("textbox", "Member ID"),
            ),
            Step(
                id="s3",
                intent="Run the search.",
                action=ClickAction(),
                target=descriptor("button", "Search"),
                checkpoint=Checkpoint(
                    id="c3",
                    description="We are on a member detail page.",
                    assertions=[
                        Predicate(
                            kind=PredicateKind.TEXT_PRESENT,
                            text="Member Detail",
                            description="detail header rendered",
                        )
                    ],
                ),
            ),
            Step(
                id="s4",
                intent="Read the savings balance off the accounts table.",
                action=ExtractAction(output="savings_balance", source="text"),
                target=descriptor("cell", "Savings Balance"),
            ),
        ],
        outcomes=[
            BusinessOutcome(
                code="MEMBER_NOT_FOUND",
                description="No member exists with the supplied id.",
                detector=Predicate(
                    kind=PredicateKind.TEXT_PRESENT,
                    text="No member found",
                ),
            )
        ],
        provenance=Provenance(
            discovery_run_id="discovery-test",
            provider="anthropic",
            model="claude-sonnet-5",
        ),
    )
