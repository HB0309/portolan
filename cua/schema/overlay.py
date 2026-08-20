"""Tenant overlays: one capability, many institutions.

The environment this system targets has hundreds of tenants running roughly
twenty apps each, and many of those tenants run *the same vendor product*,
branded and configured differently. Re-recording a capability per tenant does
not scale and does not stay in sync.

So a capability is scoped to the vendor product, and a ``TenantOverlay`` is a
narrow, reviewable patch that adapts it to one institution: a different entry
point, a renamed button, an extra confirmation step someone enabled. The base
artifact is never mutated -- ``resolve()`` returns a new effective capability,
so the shared recording stays the single source of truth and every deviation is
explicit and diffable.

Drift is detected rather than assumed: replay reports which resolution rung each
step matched on, and a tenant whose steps drift toward lower rungs is flagged for
review before it fails outright.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.schema.capability import (
    BusinessOutcome,
    Capability,
    ElementDescriptor,
    Step,
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DescriptorOverride(Strict):
    """Replace parts of one step's targeting for this tenant.

    Only the named fields are replaced; everything else -- crucially the
    fallback ladder -- is inherited, so an overlay stays small enough to review.
    """

    step_id: str
    role: str | None = None
    accessible_name: str | None = None
    name_match: Literal["exact", "normalized", "contains", "regex"] | None = None
    reason: str = Field(description="Why this tenant differs. Read during review.")

    def apply(self, descriptor: ElementDescriptor) -> ElementDescriptor:
        patch: dict[str, Any] = {}
        if self.role is not None:
            patch["role"] = self.role
        if self.accessible_name is not None:
            patch["accessible_name"] = self.accessible_name
        if self.name_match is not None:
            patch["name_match"] = self.name_match
        return descriptor.model_copy(update=patch)


class StepDisable(Strict):
    step_id: str
    reason: str


class TenantOverlay(Strict):
    tenant_id: str
    capability_id: str
    base_version: int = Field(
        description=(
            "The capability version this overlay was authored against. A mismatch "
            "means the base was re-recorded and the overlay needs re-review."
        )
    )
    entry_point: str | None = None
    descriptor_overrides: list[DescriptorOverride] = Field(default_factory=list)
    disabled_steps: list[StepDisable] = Field(default_factory=list)
    outcome_overrides: list[BusinessOutcome] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _no_duplicate_targets(self) -> TenantOverlay:
        ids = [o.step_id for o in self.descriptor_overrides]
        if len(set(ids)) != len(ids):
            raise ValueError("more than one descriptor override for the same step")
        return self


class OverlayMismatch(ValueError):
    """The overlay does not apply to the capability it was handed."""


def resolve(capability: Capability, overlay: TenantOverlay | None) -> Capability:
    """Return the effective capability for one tenant. The base is not mutated."""
    if overlay is None:
        return capability

    if overlay.capability_id != capability.capability_id:
        raise OverlayMismatch(
            f"overlay targets {overlay.capability_id!r}, "
            f"capability is {capability.capability_id!r}"
        )
    if overlay.base_version != capability.version:
        raise OverlayMismatch(
            f"overlay for tenant {overlay.tenant_id!r} was authored against "
            f"v{overlay.base_version} but the capability is v{capability.version}; "
            "re-review the overlay before replaying it"
        )

    known_steps = {s.id for s in capability.steps}
    for ref in (
        [o.step_id for o in overlay.descriptor_overrides]
        + [d.step_id for d in overlay.disabled_steps]
    ):
        if ref not in known_steps:
            raise OverlayMismatch(f"overlay references unknown step {ref!r}")

    overrides = {o.step_id: o for o in overlay.descriptor_overrides}
    disabled = {d.step_id for d in overlay.disabled_steps}

    # An overlay must not quietly remove a step that produces a declared output;
    # that would leave the capability advertising a return value it can no longer
    # deliver, which is exactly the kind of silent contract break the schema's
    # referential-integrity checks exist to prevent.
    for out in capability.outputs:
        if out.produced_by_step in disabled:
            raise OverlayMismatch(
                f"overlay disables step {out.produced_by_step!r}, which produces "
                f"declared output {out.name!r}"
            )

    steps: list[Step] = []
    for step in capability.steps:
        if step.id in disabled:
            continue
        override = overrides.get(step.id)
        if override and step.target is not None:
            step = step.model_copy(update={"target": override.apply(step.target)})
        steps.append(step)

    if not steps:
        raise OverlayMismatch("overlay disabled every step")

    outcomes = {o.code: o for o in capability.outcomes}
    for extra in overlay.outcome_overrides:
        outcomes[extra.code] = extra

    surface = capability.surface
    if overlay.entry_point:
        surface = surface.model_copy(update={"entry_point": overlay.entry_point})

    # Keep the risk profile consistent with whatever steps survived.
    surviving_risky = [s.id for s in steps if s.risk.value == "irreversible"]
    risk = capability.risk.model_copy(
        update={
            "irreversible_step_ids": surviving_risky,
            "contains_irreversible": bool(surviving_risky),
        }
    )

    return capability.model_copy(
        update={
            "surface": surface,
            "steps": steps,
            "outcomes": list(outcomes.values()),
            "risk": risk,
        }
    )
