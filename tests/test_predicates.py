"""Tests for predicate evaluation.

One evaluator answers "what is true on the screen right now" for checkpoints,
business-outcome detectors and recovery triggers alike. The case worth guarding
is scoping: this application's navigation frame carries the same link text on
every page, so an unscoped assertion can be satisfied by chrome and pass on a
page it should have failed -- which reports a broken run as a successful one.
"""

from __future__ import annotations

import pytest

from cua.replay.predicates import evaluate
from cua.schema.capability import (
    Anchor,
    AnchorKind,
    ElementDescriptor,
    Predicate,
    PredicateKind,
)
from cua.surfaces.base import Element, Observation


def element(role, name="", hint="", frame=("contentframe",), value=""):
    return Element(ref="e", role=role, name=name, label_hint=hint, frame_path=frame, value=value)


def observation(*elements, text="", url="http://127.0.0.1:8080/member"):
    return Observation(url=url, title="", elements=list(elements), text=text)


class TestText:
    def test_present(self):
        obs = observation(text="No member found for 99999")
        assert evaluate(
            Predicate(kind=PredicateKind.TEXT_PRESENT, text="No member found"), obs
        )

    def test_absent(self):
        obs = observation(text="Member Detail")
        assert evaluate(Predicate(kind=PredicateKind.TEXT_ABSENT, text="No member found"), obs)

    def test_matching_ignores_case_and_punctuation(self):
        obs = observation(text="NO MEMBER FOUND.")
        assert evaluate(
            Predicate(kind=PredicateKind.TEXT_PRESENT, text="no member found"), obs
        )

    def test_regex_mode(self):
        obs = observation(text="Reference CORESERV-500")
        assert evaluate(
            Predicate(kind=PredicateKind.TEXT_PRESENT, text=r"CORESERV-\d+", match="regex"), obs
        )


class TestScoping:
    """The bug this exists to prevent: chrome satisfying a content assertion."""

    def test_an_unscoped_assertion_is_satisfied_by_navigation_chrome(self):
        obs = observation(
            element("link", name="Member Detail", frame=("navframe",)),
            element("cell", name="Application Error"),
            text="Servicing Member Search Member Detail Application Error",
        )
        assert evaluate(
            Predicate(kind=PredicateKind.TEXT_PRESENT, text="Member Detail"), obs
        ), "unscoped assertions see the whole screen, chrome included"

    def test_a_frame_scoped_assertion_ignores_chrome(self):
        obs = observation(
            element("link", name="Member Detail", frame=("navframe",)),
            element("cell", name="Application Error"),
            text="Servicing Member Search Member Detail Application Error",
        )
        scoped = Predicate(
            kind=PredicateKind.TEXT_PRESENT,
            text="Member Detail",
            scope=[Anchor(kind=AnchorKind.FRAME, value="contentframe")],
        )
        assert not evaluate(scoped, obs), "the error page must fail this checkpoint"

    def test_a_frame_scoped_assertion_passes_on_the_right_page(self):
        obs = observation(
            element("link", name="Member Detail", frame=("navframe",)),
            element("cell", name="Member Detail"),
        )
        scoped = Predicate(
            kind=PredicateKind.TEXT_PRESENT,
            text="Member Detail",
            scope=[Anchor(kind=AnchorKind.FRAME, value="contentframe")],
        )
        assert evaluate(scoped, obs)


class TestElements:
    def test_element_present(self):
        obs = observation(element("button", name="Search"))
        predicate = Predicate(
            kind=PredicateKind.ELEMENT_PRESENT,
            target=ElementDescriptor(
                role="button", accessible_name="Search", robustness_note="t"
            ),
        )
        assert evaluate(predicate, obs)

    def test_element_absent(self):
        obs = observation(element("button", name="Cancel"))
        predicate = Predicate(
            kind=PredicateKind.ELEMENT_ABSENT,
            target=ElementDescriptor(
                role="button", accessible_name="Search", robustness_note="t"
            ),
        )
        assert evaluate(predicate, obs)

    def test_ambiguity_still_counts_as_present(self):
        """Several matches is a different problem from none.

        "Is it there" is answered yes either way; refusing to guess is the
        resolver's job when something is about to be *acted on*, not here.
        """
        obs = observation(element("button", name="Submit"), element("button", name="Submit"))
        predicate = Predicate(
            kind=PredicateKind.ELEMENT_PRESENT,
            target=ElementDescriptor(
                role="button", accessible_name="Submit", robustness_note="t"
            ),
        )
        assert evaluate(predicate, obs)


class TestUrl:
    def test_url_matches(self):
        obs = observation(url="http://127.0.0.1:8080/member?id=1")
        assert evaluate(Predicate(kind=PredicateKind.URL_MATCHES, text="/member"), obs)

    def test_url_does_not_match(self):
        obs = observation(url="http://127.0.0.1:8080/search")
        assert not evaluate(Predicate(kind=PredicateKind.URL_MATCHES, text="/member"), obs)
