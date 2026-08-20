"""The capability artifact.

A capability is a synthesized API for an application that has no API. It is
produced once by an LLM-driven discovery run and then invoked many times by
deterministic replay, with no model in the decision loop.

Three design decisions dominate this file:

1. Targets are *semantic element descriptors*, never CSS selectors and never
   coordinates. A selector recorded against a legacy enterprise app -- where ids
   are server-generated per render and test ids do not exist -- is a coin flip
   next month. A descriptor carries role, accessible name, anchoring context and
   an ordered set of fallbacks, so replay can resolve it through a ladder and
   report *how* it matched. See ``ElementDescriptor``.

2. Step values are ``ValueRef``s, not literals. A discovery run necessarily types
   concrete data into the UI. Recording that literal would persist regulated
   financial data into an artifact that gets committed and reviewed.
   Parameterization is what strips it: a typed value that came from a declared
   input is recorded as a reference to that input.

3. Business outcomes are part of the *published contract*, not an accident of the
   implementation. "No such member" is a legitimate answer the caller needs, so
   it is declared here alongside inputs and outputs rather than inferred from an
   error string at the call site.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Primitive types
# ---------------------------------------------------------------------------


class Strict(BaseModel):
    """Base model that rejects unknown fields.

    Artifacts are read back months after they were written, sometimes by a
    different version of this code. Silently dropping a field we do not
    recognise is how a capability quietly stops doing what its author intended,
    so unknown keys are an error rather than a shrug.
    """

    model_config = ConfigDict(extra="forbid")


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ENUM = "enum"
    DATE = "date"


class RiskClass(str, Enum):
    """Whether an action can be undone.

    The distinction drives policy: a SAFE action proceeds, an IRREVERSIBLE one
    is gated on the capability's approval state or escalated to a human.
    """

    SAFE = "safe"
    IRREVERSIBLE = "irreversible"


class SurfaceKind(str, Enum):
    WEB = "web"
    DESKTOP = "desktop"


class BBox(Strict):
    """Recorded on-screen geometry. The lowest and least trusted fallback."""

    x: float
    y: float
    width: float
    height: float


# ---------------------------------------------------------------------------
# Element targeting
# ---------------------------------------------------------------------------


class AnchorKind(str, Enum):
    """How a descriptor narrows the search space before matching.

    Anchors are what make a generic accessible name like "Submit" resolvable on
    a page that contains four of them.
    """

    FRAME = "frame"
    HEADING = "heading"
    LANDMARK = "landmark"
    LABEL = "label"
    CONTAINER_TEXT = "container_text"


class Anchor(Strict):
    kind: AnchorKind
    value: str


class FallbackKind(str, Enum):
    DOM_PATH = "dom_path"
    TEXT = "text"
    ATTRIBUTE = "attribute"
    BBOX = "bbox"


class FallbackHint(Strict):
    """A lower-confidence way to find the element if the semantic match fails."""

    kind: FallbackKind
    value: str = ""
    bbox: BBox | None = None

    @model_validator(mode="after")
    def _bbox_present_when_needed(self) -> FallbackHint:
        if self.kind is FallbackKind.BBOX and self.bbox is None:
            raise ValueError("bbox fallback requires a bbox")
        return self


class ElementDescriptor(Strict):
    """How replay finds a control, independent of any particular surface.

    ``role`` and ``accessible_name`` are the primary key. They were chosen
    because the same pair exists in the browser accessibility tree, in Windows
    UI Automation (``ControlType`` + ``Name``), and in the macOS accessibility
    API (``AXRole`` + ``AXTitle``). That is the seam that lets a desktop surface
    be added later without changing this schema.

    ``robustness_note`` is not decoration. The recorder must state why this
    targeting is expected to survive, and a human reviewing the capability reads
    it to decide whether to trust the automation.
    """

    role: str = Field(description="Accessibility role, e.g. button, textbox, link, cell.")
    accessible_name: str | None = Field(
        default=None,
        description="Accessible name as computed by the surface. The primary identifier.",
    )
    name_match: Literal["exact", "normalized", "contains", "regex"] = "normalized"
    anchors: list[Anchor] = Field(
        default_factory=list,
        description="Scope narrowing applied before matching, outermost first.",
    )
    ordinal: int | None = Field(
        default=None,
        description=(
            "Zero-based index among equally-matching candidates within the anchor "
            "scope. Present only when the recorder could not find a distinguishing "
            "name; its presence is itself a robustness smell."
        ),
    )
    fallbacks: list[FallbackHint] = Field(default_factory=list)
    robustness_note: str = Field(
        description="Why this targeting is expected to be stable. Reviewed by a human."
    )
    recorded_confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    def describe(self) -> str:
        """Short human-readable form, used in logs and failure reports."""
        name = f" {self.accessible_name!r}" if self.accessible_name else ""
        scope = ""
        if self.anchors:
            scope = " in " + " > ".join(f"{a.kind.value}:{a.value}" for a in self.anchors)
        ordinal = f" #{self.ordinal}" if self.ordinal is not None else ""
        return f"{self.role}{name}{ordinal}{scope}"


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


class ValueRef(Strict):
    """A value supplied to an action: either a constant or an input reference.

    Constants are things that are genuinely part of the flow (the account type a
    capability always selects). References are the caller's data. Keeping them
    distinct is what prevents recorded PII from ending up in a committed
    artifact -- see the module docstring.
    """

    kind: Literal["literal", "param"]
    literal: str | None = None
    param: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ValueRef:
        if self.kind == "literal":
            if self.literal is None:
                raise ValueError("literal ValueRef requires 'literal'")
            if self.param is not None:
                raise ValueError("literal ValueRef must not set 'param'")
        else:
            if not self.param:
                raise ValueError("param ValueRef requires 'param'")
            if self.literal is not None:
                raise ValueError("param ValueRef must not set 'literal'")
        return self

    @classmethod
    def of_literal(cls, value: str) -> ValueRef:
        return cls(kind="literal", literal=value)

    @classmethod
    def of_param(cls, name: str) -> ValueRef:
        return cls(kind="param", param=name)

    def render(self, inputs: dict[str, Any]) -> str:
        """Resolve to a concrete string for execution."""
        if self.kind == "literal":
            return self.literal or ""
        if self.param not in inputs:
            raise KeyError(f"missing required input {self.param!r}")
        return str(inputs[self.param])

    def display(self) -> str:
        """Log-safe rendering. Never resolves a parameter to its value."""
        return self.literal or "" if self.kind == "literal" else f"{{{{param.{self.param}}}}}"


# ---------------------------------------------------------------------------
# Predicates -- shared by checkpoints, outcome detectors and recovery triggers
# ---------------------------------------------------------------------------


class PredicateKind(str, Enum):
    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    ELEMENT_PRESENT = "element_present"
    ELEMENT_ABSENT = "element_absent"
    URL_MATCHES = "url_matches"
    VALUE_MATCHES = "value_matches"


class Predicate(Strict):
    """One observable condition about the current state of the surface.

    Deliberately shared between checkpoints ("did we arrive where we meant to"),
    business-outcome detectors ("is this the not-found screen") and recovery
    triggers ("is there an interstitial in the way"). They are the same kind of
    question asked for three different purposes, and sharing the evaluator means
    there is one place where "what is true right now" is decided.
    """

    kind: PredicateKind
    text: str | None = Field(
        default=None, description="Literal, substring, or regex depending on 'match'."
    )
    match: Literal["exact", "contains", "regex"] = "contains"
    target: ElementDescriptor | None = None
    scope: list[Anchor] = Field(default_factory=list)
    description: str = ""

    @model_validator(mode="after")
    def _shape(self) -> Predicate:
        needs_target = {PredicateKind.ELEMENT_PRESENT, PredicateKind.ELEMENT_ABSENT,
                        PredicateKind.VALUE_MATCHES}
        needs_text = {PredicateKind.TEXT_PRESENT, PredicateKind.TEXT_ABSENT,
                      PredicateKind.URL_MATCHES, PredicateKind.VALUE_MATCHES}
        if self.kind in needs_target and self.target is None:
            raise ValueError(f"{self.kind.value} predicate requires a target")
        if self.kind in needs_text and self.text is None:
            raise ValueError(f"{self.kind.value} predicate requires text")
        return self


class Checkpoint(Strict):
    """An assertion that we actually reached the state the step intended.

    Without this, replay is a sequence of clicks that assumes each one worked.
    A checkpoint is the difference between "we clicked Search" and "we are on
    the results page".
    """

    id: str
    description: str
    assertions: list[Predicate] = Field(min_length=1)
    timeout_ms: int = 10_000


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class WaitSpec(Strict):
    """Explicit, declarative waiting. There are no blanket sleeps in this system."""

    settle_ms: int = Field(default=250, description="Quiet period after the action.")
    timeout_ms: int = Field(default=10_000, description="Budget for the wait condition.")
    await_navigation: bool = False
    until: Predicate | None = None


class _ActionBase(Strict):
    pass


class NavigateAction(_ActionBase):
    type: Literal["navigate"] = "navigate"
    url: ValueRef


class ClickAction(_ActionBase):
    type: Literal["click"] = "click"


class TypeTextAction(_ActionBase):
    type: Literal["type_text"] = "type_text"
    value: ValueRef
    clear_first: bool = True


class SelectOptionAction(_ActionBase):
    type: Literal["select_option"] = "select_option"
    value: ValueRef


class PressKeyAction(_ActionBase):
    type: Literal["press_key"] = "press_key"
    key: str


class ExtractAction(_ActionBase):
    """Read data out of the surface and bind it to a declared output."""

    type: Literal["extract"] = "extract"
    output: str = Field(description="Name of the OutputSpec this populates.")
    source: Literal["text", "value", "attribute"] = "text"
    attribute: str | None = None
    pattern: str | None = Field(
        default=None,
        description="Optional regex; first capturing group becomes the output.",
    )


class WaitForAction(_ActionBase):
    type: Literal["wait_for"] = "wait_for"
    until: Predicate


Action = Annotated[
    Union[
        NavigateAction,
        ClickAction,
        TypeTextAction,
        SelectOptionAction,
        PressKeyAction,
        ExtractAction,
        WaitForAction,
    ],
    Field(discriminator="type"),
]

#: Actions that require an element to act on.
TARGETED_ACTIONS = {"click", "type_text", "select_option", "extract"}


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class Step(Strict):
    id: str
    intent: str = Field(
        description=(
            "Why this step exists, in the recorder's words. Lets a human audit the "
            "capability without replaying it, and gives failure reports something "
            "meaningful to name."
        )
    )
    action: Action
    target: ElementDescriptor | None = None
    wait: WaitSpec = Field(default_factory=WaitSpec)
    checkpoint: Checkpoint | None = None
    recovery_ids: list[str] = Field(default_factory=list)
    risk: RiskClass = RiskClass.SAFE
    min_confidence: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description=(
            "Resolution below this still executes but is flagged DEGRADED, which "
            "feeds the per-tenant drift signal."
        ),
    )
    approved_risky: bool = Field(
        default=False,
        description=(
            "Set by a human reviewer. An irreversible step replays unattended only "
            "if this is true AND the capability is approved."
        ),
    )

    @model_validator(mode="after")
    def _target_required_for_targeted_actions(self) -> Step:
        if self.action.type in TARGETED_ACTIONS and self.target is None:
            raise ValueError(f"step {self.id!r}: {self.action.type} requires a target")
        return self


# ---------------------------------------------------------------------------
# Outcomes and recovery
# ---------------------------------------------------------------------------


class BusinessOutcome(Strict):
    """A legitimate, non-failure result that the caller needs to know about.

    Declared here rather than discovered at the call site, because "this member
    does not exist" is part of what the capability *means*, not a malfunction.
    Conflating the two is the single most common way this kind of system is
    designed wrong.
    """

    code: str = Field(description="Stable machine code, e.g. MEMBER_NOT_FOUND.")
    description: str
    detector: Predicate
    terminal: bool = True
    outputs: dict[str, str] = Field(
        default_factory=dict,
        description="Partial outputs this outcome can still supply, if any.",
    )

    @field_validator("code")
    @classmethod
    def _upper_snake(cls, v: str) -> str:
        if not v or not v.replace("_", "").isalnum() or v != v.upper():
            raise ValueError("outcome code must be UPPER_SNAKE_CASE")
        return v


class RecoveryStrategy(str, Enum):
    DISMISS = "dismiss"
    RETRY_STEP = "retry_step"
    WAIT = "wait"
    REAUTH = "reauth"
    ESCALATE = "escalate"


class RecoveryPolicy(Strict):
    """A bounded, declarative response to a condition we know how to survive.

    Bounded matters: every strategy has an attempt budget, and exhausting it is
    a hard failure rather than an infinite loop.
    """

    id: str
    description: str
    trigger: Predicate
    strategy: RecoveryStrategy
    max_attempts: int = Field(default=2, ge=1, le=10)
    wait_ms: int = 2_000
    action: Action | None = Field(
        default=None, description="For DISMISS/REAUTH: what to do about it."
    )
    target: ElementDescriptor | None = None


# ---------------------------------------------------------------------------
# Parameters, outputs, provenance
# ---------------------------------------------------------------------------


class ParamSpec(Strict):
    name: str
    type: ParamType
    description: str
    required: bool = True
    sensitive: bool = Field(
        default=False,
        description="Never logged, never persisted, never masked-in-screenshot.",
    )
    enum_values: list[str] | None = None
    example: str | None = None

    @model_validator(mode="after")
    def _checks(self) -> ParamSpec:
        if self.type is ParamType.ENUM and not self.enum_values:
            raise ValueError(f"param {self.name!r}: enum type requires enum_values")
        if self.sensitive and self.example is not None:
            raise ValueError(f"param {self.name!r}: sensitive params must not carry an example")
        return self

    def json_schema(self) -> dict[str, Any]:
        """JSON Schema fragment, used to build agent tool definitions."""
        mapping = {
            ParamType.STRING: "string",
            ParamType.INTEGER: "integer",
            ParamType.NUMBER: "number",
            ParamType.BOOLEAN: "boolean",
            ParamType.ENUM: "string",
            ParamType.DATE: "string",
        }
        frag: dict[str, Any] = {"type": mapping[self.type], "description": self.description}
        if self.type is ParamType.ENUM and self.enum_values:
            frag["enum"] = self.enum_values
        if self.type is ParamType.DATE:
            frag["format"] = "date"
        if self.example is not None:
            frag["examples"] = [self.example]
        return frag


class OutputSpec(Strict):
    name: str
    type: ParamType
    description: str
    produced_by_step: str
    sensitive: bool = False


class Provenance(Strict):
    """Where this capability came from. Read during review, not during replay."""

    discovery_run_id: str
    provider: str
    model: str
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    steps_explored: int = 0
    human_assisted: bool = False
    human_action_count: int = 0


class RiskProfile(Strict):
    contains_irreversible: bool = False
    irreversible_step_ids: list[str] = Field(default_factory=list)
    notes: str = ""


class SurfaceBinding(Strict):
    """What this capability was recorded against.

    ``product`` is the multi-tenant join key: hundreds of institutions run the
    same vendor product, so a capability is scoped to the product, and a tenant
    overlay adapts it to one institution's instance.
    """

    kind: SurfaceKind = SurfaceKind.WEB
    entry_point: str
    product: str
    product_version: str | None = None


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


class Capability(Strict):
    """A reusable, reviewable, agent-invocable unit of work."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    capability_id: str = Field(description="Stable dotted id, e.g. cu.member.read_savings_balance.")
    version: int = Field(default=1, ge=1)
    name: str
    description: str = Field(description="Written for the calling agent, not for a developer.")
    surface: SurfaceBinding
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    outcomes: list[BusinessOutcome] = Field(default_factory=list)
    recovery: list[RecoveryPolicy] = Field(default_factory=list)
    risk: RiskProfile = Field(default_factory=RiskProfile)
    provenance: Provenance
    approval: Literal["draft", "approved"] = "draft"

    # -- integrity -------------------------------------------------------

    @model_validator(mode="after")
    def _referential_integrity(self) -> Capability:
        step_ids = [s.id for s in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("duplicate step ids")

        param_names = {p.name for p in self.inputs}
        output_names = {o.name for o in self.outputs}
        recovery_ids = {r.id for r in self.recovery}

        for step in self.steps:
            for ref in _value_refs(step.action):
                if ref.kind == "param" and ref.param not in param_names:
                    raise ValueError(
                        f"step {step.id!r} references undeclared input {ref.param!r}"
                    )
            if isinstance(step.action, ExtractAction) and step.action.output not in output_names:
                raise ValueError(
                    f"step {step.id!r} extracts into undeclared output {step.action.output!r}"
                )
            for rid in step.recovery_ids:
                if rid not in recovery_ids:
                    raise ValueError(f"step {step.id!r} references unknown recovery {rid!r}")

        for out in self.outputs:
            if out.produced_by_step not in set(step_ids):
                raise ValueError(
                    f"output {out.name!r} names unknown step {out.produced_by_step!r}"
                )

        declared = set(self.risk.irreversible_step_ids)
        actual = {s.id for s in self.steps if s.risk is RiskClass.IRREVERSIBLE}
        if declared != actual:
            raise ValueError(
                "risk.irreversible_step_ids disagrees with the steps' own risk classes; "
                f"declared={sorted(declared)} actual={sorted(actual)}"
            )
        if self.risk.contains_irreversible != bool(actual):
            raise ValueError("risk.contains_irreversible disagrees with the steps")

        outcome_codes = [o.code for o in self.outcomes]
        if len(set(outcome_codes)) != len(outcome_codes):
            raise ValueError("duplicate business outcome codes")

        return self

    # -- helpers ---------------------------------------------------------

    @property
    def slug(self) -> str:
        return f"{self.capability_id}@v{self.version}"

    def step(self, step_id: str) -> Step:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def input_spec(self, name: str) -> ParamSpec:
        for p in self.inputs:
            if p.name == name:
                return p
        raise KeyError(name)

    def sensitive_input_names(self) -> set[str]:
        return {p.name for p in self.inputs if p.sensitive}

    def input_json_schema(self) -> dict[str, Any]:
        """The capability's call signature, as JSON Schema.

        This is what makes the agent-facing catalog nearly free: a tool
        definition is this dict plus a name and a description.
        """
        return {
            "type": "object",
            "properties": {p.name: p.json_schema() for p in self.inputs},
            "required": [p.name for p in self.inputs if p.required],
            "additionalProperties": False,
        }

    def validate_inputs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Coerce and check caller-supplied inputs before the browser opens.

        Returns the coerced dict. Raises ``InputValidationError`` -- caught by
        the replay engine and returned as a structured failure, never as a
        stack trace to the caller.
        """
        coerced: dict[str, Any] = {}
        known = {p.name: p for p in self.inputs}

        for extra in set(inputs) - set(known):
            raise InputValidationError(f"unknown input {extra!r}")

        for name, spec in known.items():
            if name not in inputs or inputs[name] is None:
                if spec.required:
                    raise InputValidationError(f"missing required input {name!r}")
                continue
            coerced[name] = _coerce(spec, inputs[name])
        return coerced


class InputValidationError(ValueError):
    """Caller supplied inputs that do not satisfy the capability's contract."""


def _coerce(spec: ParamSpec, value: Any) -> Any:
    try:
        if spec.type is ParamType.INTEGER:
            return int(value)
        if spec.type is ParamType.NUMBER:
            return float(value)
        if spec.type is ParamType.BOOLEAN:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"true", "1", "yes", "y"}
        if spec.type is ParamType.ENUM:
            text = str(value)
            if spec.enum_values and text not in spec.enum_values:
                raise InputValidationError(
                    f"input {spec.name!r} must be one of {spec.enum_values}"
                )
            return text
        return str(value)
    except InputValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise InputValidationError(
            f"input {spec.name!r} is not a valid {spec.type.value}"
        ) from exc


def _value_refs(action: Any) -> list[ValueRef]:
    refs: list[ValueRef] = []
    for attr in ("url", "value"):
        ref = getattr(action, attr, None)
        if isinstance(ref, ValueRef):
            refs.append(ref)
    return refs
