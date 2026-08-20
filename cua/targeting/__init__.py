"""Finding the control a recording meant, on a page that has moved on."""

from cua.targeting.resolver import (
    LADDER,
    ResolutionError,
    ResolutionFailureKind,
    ResolvedTarget,
    Rung,
    matches_anchors,
    normalize,
    resolve,
)

__all__ = [
    "LADDER",
    "ResolutionError",
    "ResolutionFailureKind",
    "ResolvedTarget",
    "Rung",
    "matches_anchors",
    "normalize",
    "resolve",
]
