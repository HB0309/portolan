"""Turning a successful discovery run into a reusable capability.

This is the hinge of the whole system. Everything before it is a model working
something out once, expensively; everything after it is deterministic execution
that never needs a model again. The recorder is what converts one into the other,
and the quality of that conversion is what decides whether the capability still
works next month.

Three jobs, in order of how much they matter:

**Generalise the targeting.** For every acted-on element, build a descriptor that
identifies it by what it *is* rather than where it happened to be. That means
role and accessible name, anchored by frame and panel, with a written note
saying why that is expected to hold -- and never the server-generated id that
was in the DOM at the time, which will be different on the next render.

**Strip the caller's data.** The run necessarily typed a real member number into
a real field. Recording that literal would both pin the capability to one record
and persist regulated data into an artifact that gets committed and reviewed. Any
value that came from a declared input is recorded as a reference to that input,
and a final sweep refuses to emit an artifact in which one still appears.

**Attach the contract.** Checkpoints so replay can tell "the click worked" from
"we are where we meant to be", declared outputs so the caller knows what it gets,
and the product's known business outcomes and recovery policies so a legitimate
"no such member" is reported as an answer rather than a failure.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from cua.discovery.orchestrator import DiscoveryStep, DiscoveryTrace
from cua.schema.capability import (
    Anchor,
    AnchorKind,
    BBox,
    BusinessOutcome,
    Capability,
    Checkpoint,
    ClickAction,
    ElementDescriptor,
    ExtractAction,
    FallbackHint,
    FallbackKind,
    NavigateAction,
    OutputSpec,
    ParamSpec,
    ParamType,
    Predicate,
    PredicateKind,
    PressKeyAction,
    Provenance,
    RecoveryPolicy,
    RiskClass,
    RiskProfile,
    SelectOptionAction,
    Step,
    SurfaceBinding,
    TypeTextAction,
    ValueRef,
    WaitSpec,
)
from cua.surfaces.base import Element, Observation

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"

#: Frames whose controls are navigation chrome rather than the flow itself.
_CHROME_FRAMES = {"navframe"}


class RecorderError(RuntimeError):
    """The trace cannot be turned into a safe, valid capability."""


class CapabilityRecorder:
    def __init__(self, product: str, *, sensitive_inputs: set[str] | None = None) -> None:
        self.product = product
        self.sensitive_inputs = sensitive_inputs or set()
        self.knowledge = _load_knowledge(product)

    # -- descriptors -----------------------------------------------------

    def describe_element(self, element: Element) -> ElementDescriptor:
        """Build the durable identity of a control from one sighting of it.

        The subtle case is a data cell. Its accessible name is its own text --
        which is the *value*, not the identity. Recording ``cell "$4,102.55"``
        produces a capability that works for exactly one member and silently
        fails to resolve for every other, which is the worst possible failure
        shape: parameterised in the inputs, hard-coded in the targeting.

        So for cells the neighbouring label is the identity, and the value is
        deliberately not recorded at all.
        """
        is_cell = element.role in {"cell", "columnheader"}
        if is_cell and element.label_hint:
            name = element.label_hint
            by_label = True
        else:
            name = element.effective_name
            by_label = not element.name and bool(element.label_hint)

        anchors: list[Anchor] = []
        if element.frame_path:
            anchors.append(
                Anchor(kind=AnchorKind.FRAME, value="/".join(element.frame_path))
            )
        if element.section:
            anchors.append(Anchor(kind=AnchorKind.HEADING, value=element.section))

        fallbacks: list[FallbackHint] = []
        if element.row_text:
            fallbacks.append(
                FallbackHint(kind=FallbackKind.TEXT, value=element.row_text[:120])
            )
        if element.rect is not None:
            fallbacks.append(
                FallbackHint(
                    kind=FallbackKind.BBOX,
                    bbox=BBox(
                        x=element.rect.x,
                        y=element.rect.y,
                        width=element.rect.width,
                        height=element.rect.height,
                    ),
                )
            )

        return ElementDescriptor(
            role=element.role,
            accessible_name=name or None,
            name_match="normalized",
            anchors=anchors,
            fallbacks=fallbacks,
            robustness_note=self._robustness_note(element, by_label, anchors),
            recorded_confidence=0.8 if by_label else 1.0,
        )

    def _robustness_note(
        self, element: Element, by_label: bool, anchors: list[Anchor]
    ) -> str:
        parts: list[str] = []
        if element.role in {"cell", "columnheader"} and element.label_hint:
            parts.append(
                f"Data cell identified by its neighbouring label "
                f"{element.label_hint!r}. The cell's own text is the value being read "
                f"and changes with every record, so the label is the only stable "
                f"identifier; matching on the cell's text would pin this capability to "
                f"a single member."
            )
        elif by_label:
            parts.append(
                f"Field carries no accessible name of its own; identified by the "
                f"adjacent label {element.label_hint!r}, which is how this application "
                f"labels its inputs. Stable as long as the field keeps its caption."
            )
        else:
            parts.append(
                f"Matched on role plus the accessible name {element.name!r}, which is "
                f"vendor-supplied and survives tenant re-branding of layout and styling."
            )

        scopes = [a.value for a in anchors]
        if scopes:
            parts.append(
                "Anchored to " + " > ".join(scopes) + " to distinguish it from "
                "similarly named controls elsewhere on the page."
            )
        if element.frame_path and element.frame_path[-1] not in _CHROME_FRAMES:
            parts.append("Scoped to the content frame rather than navigation chrome.")
        return " ".join(parts)

    # -- values ----------------------------------------------------------

    def _value_ref(self, raw: str, inputs: dict[str, str]) -> ValueRef:
        """Record a typed value as a reference when it came from the caller.

        Exact match first, then embedded, so a deep link keeps its shape while
        still surrendering the data inside it.
        """
        for name, value in inputs.items():
            if value and raw == value:
                return ValueRef.of_param(name)

        template = raw
        substituted = False
        for name, value in inputs.items():
            if value and len(str(value)) >= 2 and str(value) in template:
                template = template.replace(str(value), f"{{{{param.{name}}}}}")
                substituted = True
        if substituted:
            return ValueRef.of_template(template)

        return ValueRef.of_literal(raw)

    # -- checkpoints -----------------------------------------------------

    def _checkpoint(
        self, step: DiscoveryStep, index: int, forbidden: set[str]
    ) -> Checkpoint | None:
        """Assert we arrived, rather than assuming the click worked.

        Only for steps that changed the screen. A checkpoint on a step that just
        filled a field asserts nothing useful and would fail for reasons that
        have nothing to do with whether the field was filled.
        """
        after = step.observation_after
        if after is None or not (step.navigated or step.tool in {"click", "press_key"}):
            return None

        marker = _distinguishing_text(after, forbidden)
        if not marker:
            return None

        return Checkpoint(
            id=f"c{index}",
            description=f"Reached the {marker!r} state.",
            assertions=[
                Predicate(
                    kind=PredicateKind.TEXT_PRESENT,
                    text=marker,
                    match="contains",
                    description=f"{marker!r} is on screen",
                )
            ],
            timeout_ms=10_000,
        )

    # -- the artifact ----------------------------------------------------

    def record(
        self,
        trace: DiscoveryTrace,
        *,
        capability_id: str,
        name: str,
        description: str,
        run_id: str,
        provider: str,
        model: str,
        input_specs: list[ParamSpec] | None = None,
        product_version: str | None = None,
        human_assisted: bool = False,
    ) -> Capability:
        if not trace.steps:
            raise RecorderError("the run performed no actions; there is nothing to record")

        inputs = input_specs or [
            ParamSpec(
                name=key,
                type=ParamType.STRING,
                description=f"Value supplied for {key}.",
                required=True,
                sensitive=key in self.sensitive_inputs,
                example=None if key in self.sensitive_inputs else value,
            )
            for key, value in trace.inputs.items()
        ]

        steps: list[Step] = []
        outputs: list[OutputSpec] = []
        recovery_ids = [policy.id for policy in self.knowledge["recovery"]]
        # Caller values must not end up in a checkpoint either: a checkpoint that
        # asserts one member's number passes for that member and reports a
        # violation for everyone else.
        forbidden_in_markers = {str(v) for v in trace.inputs.values() if str(v)}
        forbidden_in_markers |= {str(v) for v in trace.collected.values() if str(v)}

        for index, raw_step in enumerate(trace.steps, start=1):
            step_id = f"s{index}"
            descriptor = (
                self.describe_element(raw_step.element) if raw_step.element else None
            )
            action = self._action_for(raw_step, trace.inputs)

            if isinstance(action, ExtractAction):
                outputs.append(
                    OutputSpec(
                        name=action.output,
                        type=ParamType.STRING,
                        description=f"{action.output.replace('_', ' ').capitalize()} "
                        f"as displayed by the application.",
                        produced_by_step=step_id,
                    )
                )

            steps.append(
                Step(
                    id=step_id,
                    intent=raw_step.intent or f"{raw_step.tool} step {index}",
                    action=action,
                    target=descriptor,
                    wait=WaitSpec(
                        await_navigation=raw_step.navigated,
                        settle_ms=250,
                        timeout_ms=10_000,
                    ),
                    checkpoint=self._checkpoint(raw_step, index, forbidden_in_markers),
                    recovery_ids=recovery_ids,
                    risk=RiskClass(raw_step.risk),
                    min_confidence=0.7,
                    # A human approving an irreversible action during discovery
                    # approves *that* action, in that session. It does not
                    # authorise the capability to take it unattended forever
                    # after; that is a separate review, which is what the
                    # capability's approval state records.
                    approved_risky=False,
                )
            )

        irreversible = [s.id for s in steps if s.risk is RiskClass.IRREVERSIBLE]

        capability = Capability(
            capability_id=capability_id,
            version=1,
            name=name,
            description=description,
            surface=SurfaceBinding(
                entry_point=trace.entry_point,
                product=self.product,
                product_version=product_version,
            ),
            inputs=inputs,
            outputs=outputs,
            steps=steps,
            outcomes=self.knowledge["outcomes"],
            recovery=self.knowledge["recovery"],
            risk=RiskProfile(
                contains_irreversible=bool(irreversible),
                irreversible_step_ids=irreversible,
                notes=(
                    "Contains at least one irreversible step; unattended replay is "
                    "gated on review."
                )
                if irreversible
                else "All steps are read-only or reversible.",
            ),
            provenance=Provenance(
                discovery_run_id=run_id,
                provider=provider,
                model=model,
                steps_explored=len(trace.steps),
                human_assisted=human_assisted,
                human_action_count=sum(1 for s in trace.steps if s.human_approved),
            ),
            approval="draft",
        )

        self._assert_no_caller_data(capability, trace.inputs)
        return capability

    def _action_for(self, step: DiscoveryStep, inputs: dict[str, str]) -> Any:
        if step.tool == "navigate":
            return NavigateAction(url=self._value_ref(str(step.arguments["url"]), inputs))
        if step.tool == "click":
            return ClickAction()
        if step.tool == "type_text":
            return TypeTextAction(
                value=self._value_ref(str(step.arguments.get("text", "")), inputs)
            )
        if step.tool == "select_option":
            return SelectOptionAction(
                value=self._value_ref(str(step.arguments.get("option", "")), inputs)
            )
        if step.tool == "press_key":
            return PressKeyAction(key=str(step.arguments.get("key", "Enter")))
        if step.tool == "extract":
            return ExtractAction(output=str(step.arguments["output"]), source="text")
        raise RecorderError(f"cannot record tool {step.tool!r} as a step")

    def _assert_no_caller_data(self, capability: Capability, inputs: dict[str, str]) -> None:
        """The last line of defence before an artifact reaches disk.

        Parameterisation is supposed to have removed every caller value already.
        This checks rather than trusts, because the failure mode is regulated
        data committed to a public repository, and that is not something to
        discover later.
        """
        # Scoped to the steps, which is what actually gets executed and where a
        # literal would do harm. `inputs` legitimately carries a non-sensitive
        # example, and `provenance` holds identifiers we chose ourselves --
        # sweeping those in produces false positives that train people to
        # ignore this check, which is worse than not having it.
        serialized = json.dumps(
            [json.loads(step.model_dump_json()) for step in capability.steps]
        )
        for name, value in inputs.items():
            text = str(value)
            if len(text) >= 3 and text in serialized:
                raise RecorderError(
                    f"refusing to emit the capability: the value of input {name!r} "
                    f"appears literally in a recorded step. It must be recorded as a "
                    f"parameter reference."
                )


def _looks_like_data(text: str) -> bool:
    """Is this a value rather than a label?

    A checkpoint has to assert something about the *screen*, not about the
    record that happened to be on it. "Member Detail" is a state; "$4,102.55" is
    one member's balance, and asserting it would produce a capability that
    verifies successfully for exactly one input and reports a checkpoint
    violation for every other.
    """
    stripped = re.sub(r"[\s.,$%/:-]", "", text)
    if not stripped:
        return True
    digits = sum(character.isdigit() for character in stripped)
    return digits / len(stripped) > 0.4


def _distinguishing_text(observation: Observation, forbidden: set[str]) -> str:
    """Pick the phrase that best identifies the state we just reached.

    Panel titles are the most reliable marker in this class of application: they
    name the screen, they are short, and they change when the screen does.

    Two categories are excluded outright. Navigation chrome is identical on every
    page and would make a checkpoint that passes everywhere. And anything
    containing the caller's data, or that looks like a value rather than a label,
    would pin the checkpoint to one record -- the same failure shape as recording
    a data cell by its own text.
    """

    def usable(candidate: str) -> bool:
        candidate = candidate.strip()
        if not (3 <= len(candidate) <= 60):
            return False
        if any(value and str(value) in candidate for value in forbidden):
            return False
        return not _looks_like_data(candidate)

    for element in observation.elements:
        if element.frame_path and element.frame_path[-1] in _CHROME_FRAMES:
            continue
        if usable(element.section or ""):
            return element.section.strip()

    for element in observation.elements:
        if element.frame_path and element.frame_path[-1] in _CHROME_FRAMES:
            continue
        if usable(element.name or ""):
            return element.name.strip()
    return ""


def _load_knowledge(product: str) -> dict[str, Any]:
    """Load the curated outcomes and recovery policies for a vendor product."""
    path = KNOWLEDGE_DIR / f"{product}.yaml"
    if not path.exists():
        return {"outcomes": [], "recovery": []}

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    outcomes = [BusinessOutcome.model_validate(item) for item in raw.get("outcomes", [])]
    recovery = [RecoveryPolicy.model_validate(item) for item in raw.get("recovery", [])]
    return {"outcomes": outcomes, "recovery": recovery}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
