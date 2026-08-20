"""The surface abstraction.

A *surface* is anything that can be perceived and acted upon the way a human
operator would: a browser page, a legacy frameset, a native desktop window. This
module defines the contract, and it is deliberately the narrowest interface that
still supports the whole system:

    observe()   -> a normalized element graph plus context
    act()       -> perform one typed action
    screenshot() / snapshot() -> evidence

Everything above this line -- the artifact schema, the resolution ladder, the
replay engine, the escalation model -- is written against this interface and
knows nothing about Playwright. That is the seam that makes heterogeneity work: the
recorded flow describes *what to do to which control*, and the surface describes
*how perceiving and acting actually happen* on one kind of application.

Adding a desktop surface is therefore an implementation of this interface, not a
change to the schema. ``Element.role`` and ``Element.name`` map onto Windows UI
Automation's ``ControlType``/``Name`` and macOS accessibility's
``AXRole``/``AXTitle`` without translation, which is the reason those two fields
were chosen as the primary identifier in the first place.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


@dataclass
class Element:
    """One perceivable, potentially actionable control.

    ``handle`` is the only surface-specific field. It is whatever the concrete
    surface needs to act on this element again (an element id for the browser,
    a UIA runtime id for a desktop app) and it is *never* persisted into an
    artifact -- it is valid only for the observation that produced it.
    """

    ref: str
    role: str
    name: str = ""
    value: str = ""
    #: Text of the nearest label-like neighbour when the element has no
    #: accessible name of its own. Legacy forms label fields by table-cell
    #: adjacency rather than markup, so without this an entire class of input is
    #: unaddressable.
    label_hint: str = ""
    enabled: bool = True
    visible: bool = True
    focused: bool = False
    #: Frame path from the top document, outermost first. Empty on the main
    #: document; on a frameset this is what distinguishes two identical controls.
    frame_path: tuple[str, ...] = ()
    #: Text of the nearest enclosing section heading or panel title.
    section: str = ""
    #: For table cells: the text of the row they sit in.
    row_text: str = ""
    rect: Rect | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    handle: Any = None

    @property
    def effective_name(self) -> str:
        """The name to match on: the accessible name, or the label standing in."""
        return self.name or self.label_hint

    def describe(self) -> str:
        bits = [self.role]
        if self.effective_name:
            bits.append(repr(self.effective_name))
        if self.value:
            bits.append(f"value={self.value!r}")
        if self.frame_path:
            bits.append("frame=" + "/".join(self.frame_path))
        if self.section:
            bits.append(f"in {self.section!r}")
        return " ".join(bits)


@dataclass
class Observation:
    """A single perception of the surface at one moment.

    This is what the agent reasons over and what the resolver searches. It is
    intentionally a flat list rather than a tree: every consumer wants to filter
    and match, none of them want to walk, and the hierarchy that matters is
    preserved in ``frame_path``, ``section`` and ``row_text``.
    """

    url: str
    title: str
    elements: list[Element] = field(default_factory=list)
    text: str = ""
    step_index: int = 0

    def actionable(self) -> list[Element]:
        return [e for e in self.elements if e.visible and e.enabled]

    def find_role(self, role: str) -> list[Element]:
        return [e for e in self.elements if e.role == role]

    def by_ref(self, ref: str) -> Element | None:
        for element in self.elements:
            if element.ref == ref:
                return element
        return None


@dataclass
class ActionRequest:
    """A single thing to do. Surface-agnostic by construction."""

    type: str
    ref: str | None = None
    url: str | None = None
    text: str | None = None
    key: str | None = None
    option: str | None = None


@dataclass
class ActionOutcome:
    ok: bool
    detail: str = ""
    extracted: str | None = None
    navigated: bool = False


class SurfaceError(RuntimeError):
    """The surface itself failed -- crashed, disconnected, or refused to act."""


class Surface(ABC):
    """What every kind of application must be able to do to be automatable."""

    kind: str = "abstract"

    @abstractmethod
    async def observe(self) -> Observation:
        """Perceive the current state as a normalized element graph."""

    @abstractmethod
    async def act(self, request: ActionRequest) -> ActionOutcome:
        """Perform one action. Implementations must assert control ownership."""

    @abstractmethod
    async def screenshot(self, path: str, mask_refs: list[str] | None = None) -> str:
        """Capture evidence. ``mask_refs`` are painted over before capture."""

    @abstractmethod
    async def snapshot(self, path: str) -> str:
        """Persist a richer dump (markup, tree) for failure diagnosis."""

    @abstractmethod
    async def current_url(self) -> str:
        ...
