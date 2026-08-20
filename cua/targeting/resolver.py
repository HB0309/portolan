"""The resolution ladder.

Given a recorded ``ElementDescriptor`` and a fresh ``Observation``, find the one
control the recording meant -- or refuse.

The ladder is tried in order and stops at the first rung that yields exactly one
candidate. Which rung matched is reported back, and that number does real work
beyond this module:

* It is the confidence attached to the step. Matching below the step's floor
  still executes but is marked DEGRADED.
* It is the drift signal. A tenant whose steps increasingly resolve on lower
  rungs is diverging from the recording, and can be flagged for review before it
  breaks outright. That is how per-tenant drift is detected without anyone
  diffing screenshots.

The rule that matters most is the one at the bottom of ``resolve``: **more than
one candidate at the winning rung is a failure, not a choice.** In a banking
back-office the cost of activating the wrong control is unbounded and often
irreversible, while the cost of refusing is one human interruption. Any strategy
that quietly picks the first match is trading an unbounded risk for a trivial
convenience.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from cua.schema.capability import Anchor, AnchorKind, ElementDescriptor, FallbackKind
from cua.schema.results import Resolution
from cua.surfaces.base import Element, Observation


class ResolutionFailureKind(str, Enum):
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"


class ResolutionError(Exception):
    def __init__(
        self,
        kind: ResolutionFailureKind,
        descriptor: ElementDescriptor,
        message: str,
        candidates: list[Element] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.descriptor = descriptor
        self.candidates = candidates or []

    def observed(self) -> str:
        if not self.candidates:
            return "no element matched at any rung"
        return "; ".join(c.describe() for c in self.candidates[:5])


@dataclass
class ResolvedTarget:
    element: Element
    resolution: Resolution


@dataclass(frozen=True)
class Rung:
    number: int
    name: str
    confidence: float


#: Ordered best to worst. Each rung is a genuinely different question, not the
#: same one with a looser threshold.
LADDER: tuple[Rung, ...] = (
    Rung(1, "role + exact accessible name", 1.00),
    Rung(2, "role + normalized accessible name", 0.90),
    Rung(3, "role + adjacent label (legacy form)", 0.80),
    Rung(4, "role + partial or pattern name", 0.70),
    Rung(5, "role + section/row anchor", 0.55),
    Rung(6, "name match ignoring role", 0.40),
    Rung(7, "recorded bounding box", 0.20),
)

_PUNCT = re.compile(r"[^a-z0-9]+")


def normalize(text: str | None) -> str:
    """Fold the differences that never mean anything.

    Case, whitespace, punctuation and currency symbols vary between tenant
    configurations of the same product without the control changing identity.
    What survives here is what the control is actually *called*.
    """
    if not text:
        return ""
    return _PUNCT.sub("", text.strip().lower())


def _matches_anchor(element: Element, anchor: Anchor) -> bool:
    needle = normalize(anchor.value)
    if not needle:
        return True
    if anchor.kind is AnchorKind.FRAME:
        return needle in normalize("/".join(element.frame_path))
    if anchor.kind in (AnchorKind.HEADING, AnchorKind.LANDMARK):
        return needle in normalize(element.section)
    if anchor.kind is AnchorKind.LABEL:
        return needle in normalize(element.label_hint)
    if anchor.kind is AnchorKind.CONTAINER_TEXT:
        haystack = normalize(element.row_text) + normalize(element.section)
        return needle in haystack
    return True


def matches_anchors(element: Element, anchors: list[Anchor]) -> bool:
    """Every anchor must hold. Anchors narrow; they do not vote."""
    return all(_matches_anchor(element, anchor) for anchor in anchors)


def in_scope(element: Element, descriptor: ElementDescriptor) -> bool:
    return matches_anchors(element, descriptor.anchors)


def _name_predicate(descriptor: ElementDescriptor, candidate_name: str) -> bool:
    """Compare a candidate's name against the descriptor's, per its match mode."""
    target = descriptor.accessible_name or ""
    if descriptor.name_match == "exact":
        return candidate_name == target
    if descriptor.name_match == "regex":
        try:
            return re.search(target, candidate_name, re.IGNORECASE) is not None
        except re.error:
            return False
    if descriptor.name_match == "contains":
        return normalize(target) in normalize(candidate_name)
    return normalize(candidate_name) == normalize(target)


def _primary(element: Element, descriptor: ElementDescriptor) -> str:
    """The string this descriptor considers the element's identity.

    For most controls that is the accessible name. For one identified by
    adjacency -- a form field with no name, or a data cell whose own text is the
    value being read -- it is the neighbouring caption. Getting this backwards
    resolves 'Current Savings Balance' to the label cell and returns the label
    as the answer, so the descriptor states which it means rather than letting
    the ladder guess.
    """
    if descriptor.name_source == "adjacent_label":
        return element.label_hint
    return element.name


def _secondary(element: Element, descriptor: ElementDescriptor) -> str:
    if descriptor.name_source == "adjacent_label":
        return element.name
    return element.label_hint


def _rung_candidates(
    rung: Rung, descriptor: ElementDescriptor, scoped: list[Element]
) -> list[Element]:
    target = descriptor.accessible_name or ""
    want_role = descriptor.role

    if rung.number == 1:
        return [e for e in scoped if e.role == want_role and _primary(e, descriptor) == target]

    if rung.number == 2:
        return [
            e
            for e in scoped
            if e.role == want_role
            and normalize(_primary(e, descriptor)) == normalize(target)
        ]

    if rung.number == 3:
        # The other identifier: a control's own name when it is anchored by
        # adjacency, or the adjacent caption when it is named. Lower confidence
        # because it is not what the recording said it meant.
        return [
            e
            for e in scoped
            if e.role == want_role
            and normalize(_secondary(e, descriptor)) == normalize(target)
        ]

    if rung.number == 4:
        return [
            e
            for e in scoped
            if e.role == want_role
            and (_name_predicate(descriptor, e.name) or _name_predicate(descriptor, e.label_hint))
        ]

    if rung.number == 5:
        needle = normalize(target)
        if not needle:
            return []
        return [
            e
            for e in scoped
            if e.role == want_role
            and (needle in normalize(e.row_text) or needle in normalize(e.section))
        ]

    if rung.number == 6:
        # Role drift: a vendor upgrade turned a link into a button. The name is
        # still the identity, so accept it, loudly and at low confidence.
        return [
            e
            for e in scoped
            if normalize(e.name) == normalize(target)
            or normalize(e.label_hint) == normalize(target)
        ]

    return []


def _bbox_candidates(descriptor: ElementDescriptor, scoped: list[Element]) -> list[Element]:
    """Last resort: whatever now sits where the control used to be.

    Off unless policy explicitly enables it, because a tenant re-theming the
    same vendor product moves every pixel while changing no control's identity.
    """
    hint = next((f for f in descriptor.fallbacks if f.kind is FallbackKind.BBOX), None)
    if hint is None or hint.bbox is None:
        return []

    want_x, want_y = hint.bbox.x + hint.bbox.width / 2, hint.bbox.y + hint.bbox.height / 2
    hits: list[tuple[float, Element]] = []
    for element in scoped:
        if element.rect is None:
            continue
        cx, cy = element.rect.center()
        distance = ((cx - want_x) ** 2 + (cy - want_y) ** 2) ** 0.5
        if distance <= 40:
            hits.append((distance, element))
    if not hits:
        return []
    hits.sort(key=lambda pair: pair[0])
    return [hits[0][1]]


def resolve(
    descriptor: ElementDescriptor,
    observation: Observation,
    *,
    allow_bbox: bool = False,
    min_confidence: float | None = None,
) -> ResolvedTarget:
    """Find the one element this descriptor means, or raise.

    Raises ``ResolutionError`` with kind NOT_FOUND if no rung matched, or
    AMBIGUOUS if a rung matched more than one candidate and the descriptor did
    not record an ordinal to disambiguate with.
    """
    scoped = [e for e in observation.actionable() if in_scope(e, descriptor)]
    if not scoped:
        # Anchors may reference a section that is not on this page at all. Fall
        # back to the whole observation so the failure message can say what *was*
        # there rather than nothing.
        scoped = observation.actionable()

    near_misses: list[Element] = []

    for rung in LADDER:
        if rung.number == 7:
            if not allow_bbox:
                continue
            candidates = _bbox_candidates(descriptor, scoped)
        else:
            candidates = _rung_candidates(rung, descriptor, scoped)

        if not candidates:
            continue

        if len(candidates) > 1:
            if descriptor.ordinal is not None and descriptor.ordinal < len(candidates):
                chosen = candidates[descriptor.ordinal]
            else:
                raise ResolutionError(
                    ResolutionFailureKind.AMBIGUOUS,
                    descriptor,
                    (
                        f"{len(candidates)} controls match {descriptor.describe()} at rung "
                        f"{rung.number} ({rung.name}); refusing to guess"
                    ),
                    candidates,
                )
        else:
            chosen = candidates[0]

        degraded = min_confidence is not None and rung.confidence < min_confidence
        return ResolvedTarget(
            element=chosen,
            resolution=Resolution(
                rung=rung.number,
                rung_name=rung.name,
                confidence=rung.confidence,
                candidates_considered=len(candidates),
                degraded=degraded,
            ),
        )

    # Nothing matched. Offer the closest things by role so the failure report can
    # say what was on the page instead of a bare "not found".
    near_misses = [e for e in scoped if e.role == descriptor.role][:5]
    raise ResolutionError(
        ResolutionFailureKind.NOT_FOUND,
        descriptor,
        f"no control matched {descriptor.describe()} at any rung",
        near_misses,
    )
