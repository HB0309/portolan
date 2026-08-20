"""Evaluating a predicate against the current state.

One evaluator serves checkpoints, business-outcome detectors and recovery
triggers. They ask the same kind of question -- what is true on the screen right
now -- for three different purposes, and sharing the implementation means there
is exactly one place where that question is answered. Three near-identical
matchers that drift apart is how a system ends up detecting "record not found"
in the outcome path but not in the recovery path.
"""

from __future__ import annotations

import re

from cua.schema.capability import Predicate, PredicateKind
from cua.surfaces.base import Observation
from cua.targeting.resolver import ResolutionError, matches_anchors, normalize, resolve


def _text_matches(haystack: str, needle: str, mode: str) -> bool:
    if mode == "exact":
        return haystack.strip() == needle.strip()
    if mode == "regex":
        try:
            return re.search(needle, haystack, re.IGNORECASE | re.DOTALL) is not None
        except re.error:
            return False
    return normalize(needle) in normalize(haystack)


def _scoped_text(predicate: Predicate, observation: Observation) -> str:
    """The text the predicate should look at.

    Without a scope this is the whole screen. With one, only the matching
    region -- which matters more than it sounds. This application's navigation
    frame contains a link reading "Member Detail" on every single page, so an
    unscoped assertion that "Member Detail" is present passes on the error
    screen too, and reports a broken run as a successful one.
    """
    if not predicate.scope:
        return observation.text

    return "\n".join(
        " ".join(
            part
            for part in (element.name, element.label_hint, element.row_text, element.value)
            if part
        )
        for element in observation.elements
        if matches_anchors(element, predicate.scope)
    )


def evaluate(predicate: Predicate, observation: Observation) -> bool:
    kind = predicate.kind

    if kind in (PredicateKind.TEXT_PRESENT, PredicateKind.TEXT_ABSENT):
        found = _text_matches(_scoped_text(predicate, observation), predicate.text or "",
                              predicate.match)
        return found if kind is PredicateKind.TEXT_PRESENT else not found

    if kind is PredicateKind.URL_MATCHES:
        return _text_matches(observation.url, predicate.text or "", predicate.match)

    if kind in (PredicateKind.ELEMENT_PRESENT, PredicateKind.ELEMENT_ABSENT):
        assert predicate.target is not None
        try:
            resolve(predicate.target, observation)
            present = True
        except ResolutionError:
            # Ambiguity counts as present: several matching controls is not the
            # same problem as none, and an "is it there" question is answered
            # yes either way.
            present = _any_candidate(predicate, observation)
        return present if kind is PredicateKind.ELEMENT_PRESENT else not present

    if kind is PredicateKind.VALUE_MATCHES:
        assert predicate.target is not None
        try:
            found = resolve(predicate.target, observation)
        except ResolutionError:
            return False
        actual = found.element.value or found.element.name
        return _text_matches(actual, predicate.text or "", predicate.match)

    return False


def _any_candidate(predicate: Predicate, observation: Observation) -> bool:
    target = predicate.target
    assert target is not None
    wanted = normalize(target.accessible_name or "")
    return any(
        element.role == target.role
        and (normalize(element.name) == wanted or normalize(element.label_hint) == wanted)
        for element in observation.elements
    )


def describe(predicate: Predicate) -> str:
    if predicate.description:
        return predicate.description
    if predicate.target is not None:
        return f"{predicate.kind.value} {predicate.target.describe()}"
    return f"{predicate.kind.value} {predicate.text!r}"
