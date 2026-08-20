"""The desktop surface seam -- deliberately not implemented.

This file exists to make the extension point concrete rather than to claim
support that is not there. It is documented as a cut in REPORT.md.

The reason a desktop surface needs no schema change is that the artifact's
primary identifier -- role plus accessible name -- already exists on both
platforms:

    ElementDescriptor.role            Windows UIA ControlType   macOS AXRole
    ElementDescriptor.accessible_name Windows UIA Name          macOS AXTitle
    Element.frame_path                window / pane hierarchy   AXWindow chain
    Element.section                   enclosing group's Name    AXGroup title

So implementing this class is a matter of populating ``Observation`` from
``UIAutomationClient`` (via comtypes or pywinauto) on Windows, or from the
AXUIElement APIs on macOS, and mapping ``ActionRequest`` onto Invoke/Value
patterns. The resolution ladder, the artifact schema, the replay engine, the
policy gate and the escalation model are untouched by that work, which is the
property the abstraction was chosen for.

Two rungs of the ladder do not carry over unchanged and are noted here so the
limitation is not discovered later:

* ``dom_path`` has no desktop analogue; the equivalent is the control's index
  path within its parent pane, which is what a desktop implementation would
  populate that fallback with.
* ``bbox`` works, but is if anything less trustworthy than on the web because
  desktop windows are resizable by the operator.
"""

from __future__ import annotations

from cua.surfaces.base import ActionOutcome, ActionRequest, Observation, Surface


class DesktopSurface(Surface):
    kind = "desktop"

    def __init__(self, *_: object, **__: object) -> None:
        raise NotImplementedError(
            "The desktop surface is a documented seam, not an implementation. "
            "See the module docstring and REPORT.md section 4."
        )

    async def observe(self) -> Observation:  # pragma: no cover - not implemented
        raise NotImplementedError

    async def act(self, request: ActionRequest) -> ActionOutcome:  # pragma: no cover
        raise NotImplementedError

    async def screenshot(self, path: str, mask_refs: list[str] | None = None) -> str:  # pragma: no cover
        raise NotImplementedError

    async def snapshot(self, path: str) -> str:  # pragma: no cover
        raise NotImplementedError

    async def current_url(self) -> str:  # pragma: no cover
        raise NotImplementedError
