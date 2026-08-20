"""Tests for the resolution ladder.

The behaviour worth guarding here is not "it finds things" -- it is which rung
it finds them on, and that it refuses when the answer is not unique.
"""

from __future__ import annotations

import pytest

from cua.schema.capability import Anchor, AnchorKind, BBox, ElementDescriptor, FallbackHint, FallbackKind
from cua.surfaces.base import Element, Observation, Rect
from cua.targeting import ResolutionError, ResolutionFailureKind, normalize, resolve


def el(ref, role, name="", hint="", section="", row="", frame=(), rect=None):
    return Element(
        ref=ref, role=role, name=name, label_hint=hint, section=section,
        row_text=row, frame_path=frame, rect=rect,
    )


def desc(role, name, **kw):
    kw.setdefault("robustness_note", "test")
    return ElementDescriptor(role=role, accessible_name=name, **kw)


def obs(*elements):
    return Observation(url="http://x/", title="t", elements=list(elements))


class TestRungs:
    def test_rung_1_exact_name(self):
        o = obs(el("a", "button", name="Search"))
        r = resolve(desc("button", "Search", name_match="exact"), o)
        assert r.element.ref == "a"
        assert r.resolution.rung == 1

    def test_rung_2_normalized_name(self):
        """Punctuation and case differ between tenant configurations."""
        o = obs(el("a", "button", name="open sub account"))
        r = resolve(desc("button", "Open Sub-Account"), o)
        assert r.resolution.rung == 2
        assert r.element.ref == "a"

    def test_rung_3_adjacent_label(self):
        """The legacy case: the field has no name, only a neighbouring cell."""
        o = obs(el("a", "textbox", name="", hint="Member Number"))
        r = resolve(desc("textbox", "Member Number"), o)
        assert r.resolution.rung == 3

    def test_rung_4_partial_name(self):
        o = obs(el("a", "button", name="Search Members Now"))
        r = resolve(desc("button", "Search", name_match="contains"), o)
        assert r.resolution.rung == 4

    def test_rung_5_row_anchor(self):
        o = obs(el("a", "cell", name="$4,102.55", row="Current Savings Balance $4,102.55"))
        r = resolve(desc("cell", "Current Savings Balance"), o)
        assert r.resolution.rung == 5

    def test_rung_6_ignores_role_drift(self):
        """A vendor upgrade turned the link into a button; identity is the name."""
        o = obs(el("a", "button", name="Open Sub-Account"))
        r = resolve(desc("link", "Open Sub-Account"), o)
        assert r.resolution.rung == 6
        assert r.resolution.confidence == 0.40

    def test_rung_7_bbox_is_off_by_default(self):
        d = desc("button", "Nowhere", fallbacks=[
            FallbackHint(kind=FallbackKind.BBOX, bbox=BBox(x=10, y=10, width=20, height=10))
        ])
        o = obs(el("a", "button", name="Different", rect=Rect(10, 10, 20, 10)))
        with pytest.raises(ResolutionError) as exc:
            resolve(d, o)
        assert exc.value.kind is ResolutionFailureKind.NOT_FOUND

    def test_rung_7_bbox_when_policy_allows(self):
        d = desc("button", "Nowhere", fallbacks=[
            FallbackHint(kind=FallbackKind.BBOX, bbox=BBox(x=10, y=10, width=20, height=10))
        ])
        o = obs(el("a", "button", name="Different", rect=Rect(10, 10, 20, 10)))
        r = resolve(d, o, allow_bbox=True)
        assert r.resolution.rung == 7
        assert r.resolution.confidence == 0.20


class TestRefusal:
    def test_ambiguous_is_a_failure_not_a_choice(self):
        """The rule the whole module exists for."""
        o = obs(
            el("a", "button", name="Submit"),
            el("b", "button", name="Submit"),
        )
        with pytest.raises(ResolutionError) as exc:
            resolve(desc("button", "Submit"), o)
        assert exc.value.kind is ResolutionFailureKind.AMBIGUOUS
        assert len(exc.value.candidates) == 2

    def test_anchor_disambiguates_what_would_otherwise_be_ambiguous(self):
        o = obs(
            el("a", "button", name="Submit", section="Member Search"),
            el("b", "button", name="Submit", section="Sub-Account"),
        )
        d = desc("button", "Submit", anchors=[Anchor(kind=AnchorKind.HEADING, value="Sub-Account")])
        assert resolve(d, o).element.ref == "b"

    def test_frame_anchor_separates_identical_controls(self):
        o = obs(
            el("a", "link", name="Member Detail", frame=("navframe",)),
            el("b", "link", name="Member Detail", frame=("contentframe",)),
        )
        d = desc("link", "Member Detail", anchors=[Anchor(kind=AnchorKind.FRAME, value="contentframe")])
        assert resolve(d, o).element.ref == "b"

    def test_ordinal_resolves_ambiguity_when_recorded(self):
        o = obs(el("a", "button", name="Go"), el("b", "button", name="Go"))
        assert resolve(desc("button", "Go", ordinal=1), o).element.ref == "b"

    def test_not_found_reports_near_misses(self):
        o = obs(el("a", "button", name="Cancel"), el("b", "button", name="Back"))
        with pytest.raises(ResolutionError) as exc:
            resolve(desc("button", "Transfer Funds"), o)
        assert exc.value.kind is ResolutionFailureKind.NOT_FOUND
        assert "Cancel" in exc.value.observed()


class TestDegradation:
    def test_low_rung_below_floor_is_marked_degraded(self):
        o = obs(el("a", "button", name="Open Sub-Account"))
        r = resolve(desc("link", "Open Sub-Account"), o, min_confidence=0.7)
        assert r.resolution.degraded is True

    def test_high_rung_is_not_degraded(self):
        o = obs(el("a", "button", name="Search"))
        r = resolve(desc("button", "Search"), o, min_confidence=0.7)
        assert r.resolution.degraded is False


class TestNormalize:
    @pytest.mark.parametrize("a,b", [
        ("Open Sub-Account", "open sub account"),
        ("  Search  ", "search"),
        ("Member #", "member"),
        ("$4,102.55", "410255"),
    ])
    def test_folds_meaningless_differences(self, a, b):
        assert normalize(a) == normalize(b)

    def test_keeps_meaningful_differences(self):
        assert normalize("Open Sub-Account") != normalize("Create Sub Account")
