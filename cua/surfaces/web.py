"""The browser surface.

Implements ``Surface`` on top of Playwright. Two properties of this
implementation are worth stating, because both are consequences of the
environment rather than of Playwright:

**It traverses frames.** The target class of application still uses framesets,
so an observation is the union of every frame's element graph, with each element
tagged by the frame path it was found in. Two controls with identical names in
the nav frame and the content frame are distinguishable, which they would not be
if we only ever looked at the main document.

**It never returns a selector.** Perception hands back opaque per-observation
refs. Nothing that leaves this module can be persisted into an artifact and
replayed as a selector, which makes the "descriptors, not selectors" rule
structural rather than a matter of discipline.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Frame,
    Page,
    Playwright,
    async_playwright,
)

from cua.surfaces.base import (
    ActionOutcome,
    ActionRequest,
    Element,
    Observation,
    Rect,
    Surface,
    SurfaceError,
)

_EXTRACT_JS = (Path(__file__).resolve().parent.parent / "perception" / "extract.js").read_text(
    encoding="utf-8"
)


class WebSurface(Surface):
    kind = "web"

    def __init__(
        self,
        context: BrowserContext,
        page: Page,
        *,
        control_check: Any = None,
        playwright: Playwright | None = None,
        browser: Browser | None = None,
    ) -> None:
        self._context = context
        self._page = page
        self._playwright = playwright
        self._browser = browser
        #: Callable raising if the caller does not currently own control.
        #: Injected by the session manager; see cua.session.
        self._control_check = control_check
        self._step_index = 0

    # -- lifecycle -------------------------------------------------------

    @classmethod
    async def launch(
        cls, *, headless: bool = False, viewport: tuple[int, int] = (1280, 900)
    ) -> WebSurface:
        """Start a browser and hand back a surface bound to one page.

        Headed by default. The human handoff requires a window a person can
        actually operate, and a session that only exists in a headless process
        is not one control can be transferred over.
        """
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]}
        )
        page = await context.new_page()
        return cls(context, page, playwright=playwright, browser=browser)

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except PlaywrightError:
                    pass
        if self._playwright is not None:
            await self._playwright.stop()

    @property
    def page(self) -> Page:
        return self._page

    @property
    def context(self) -> BrowserContext:
        return self._context

    def bind_control_check(self, check: Any) -> None:
        self._control_check = check

    def _assert_control(self, action: str) -> None:
        if self._control_check is not None:
            self._control_check(action)

    # -- perception ------------------------------------------------------

    def _frames(self) -> list[tuple[tuple[str, ...], Frame]]:
        """Every frame, in a stable order, with the path that identifies it.

        The path uses frame *names* because that is what a descriptor anchors on
        and what stays meaningful across renders. Names are not guaranteed to be
        present or unique, though -- a frame that has not finished attaching
        reports neither a name nor a URL -- so the positional index is included
        as a fallback segment. Without it, two unnamed frames collapse onto the
        same path and their element refs collide silently, which is a great deal
        worse than an ugly path.
        """
        found: list[tuple[tuple[str, ...], Frame]] = []

        def walk(frame: Frame, path: tuple[str, ...]) -> None:
            found.append((path, frame))
            for index, child in enumerate(frame.child_frames):
                name = child.name or child.url.rsplit("/", 1)[-1] or f"f{index}"
                walk(child, path + (name,))

        walk(self._page.main_frame, ())
        return found

    async def observe(self) -> Observation:
        elements: list[Element] = []
        texts: list[str] = []
        url = self._page.url
        title = ""

        for frame_index, (path, frame) in enumerate(self._frames()):
            try:
                payload = await frame.evaluate(_EXTRACT_JS)
            except PlaywrightError:
                # A frame can detach between enumeration and evaluation during a
                # navigation. That is normal, not a failure of perception.
                continue
            if not payload:
                continue

            if not path:
                title = payload.get("title") or title

            frame_text = (payload.get("text") or "").strip()
            if frame_text:
                label = "/".join(path) if path else "main"
                texts.append(f"[{label}]\n{frame_text}")

            for raw in payload.get("elements", []):
                rect_raw = raw.get("rect") or {}
                elements.append(
                    Element(
                        # Refs are positional so they are unique even when two
                        # frames report the same name; they live only as long as
                        # this observation and never reach an artifact.
                        ref=f"{frame_index}:{raw['ref']}",
                        role=raw.get("role", "generic"),
                        name=raw.get("name", ""),
                        value=raw.get("value", ""),
                        label_hint=raw.get("label_hint", ""),
                        enabled=bool(raw.get("enabled", True)),
                        visible=bool(raw.get("visible", True)),
                        focused=bool(raw.get("focused", False)),
                        frame_path=path,
                        section=raw.get("section", ""),
                        row_text=raw.get("row_text", ""),
                        rect=Rect(**rect_raw) if rect_raw else None,
                        attributes=raw.get("attributes", {}) or {},
                        handle=(path, raw["ref"]),
                    )
                )

        self._step_index += 1
        return Observation(
            url=url,
            title=title,
            elements=elements,
            text="\n\n".join(texts),
            step_index=self._step_index,
        )

    async def current_url(self) -> str:
        return self._page.url

    # -- action ----------------------------------------------------------

    async def _locate(self, element_ref: str):
        """Resolve an observation ref back to a live locator.

        Refs are only meaningful for the observation that produced them, so this
        deliberately fails loudly rather than searching for something similar: if
        the page moved underneath us, the caller needs to observe again, not act
        on a guess.
        """
        frame_part, _, local_ref = element_ref.partition(":")
        try:
            frame_index = int(frame_part)
        except ValueError as exc:
            raise SurfaceError(f"malformed element reference {element_ref!r}") from exc

        frames = self._frames()
        if frame_index >= len(frames):
            raise SurfaceError(
                f"frame {frame_index} is no longer present "
                f"({len(frames)} frames on the page); re-observe before acting"
            )

        locator = frames[frame_index][1].locator(f"[data-cua-ref='{local_ref}']")
        if await locator.count() == 0:
            raise SurfaceError(f"element {element_ref} is no longer on the page")
        return locator.first

    async def act(self, request: ActionRequest) -> ActionOutcome:
        self._assert_control(request.type)
        try:
            return await self._act(request)
        except SurfaceError:
            raise
        except PlaywrightError as exc:
            raise SurfaceError(str(exc).splitlines()[0]) from exc

    async def _act(self, request: ActionRequest) -> ActionOutcome:
        if request.type == "navigate":
            if not request.url:
                raise SurfaceError("navigate requires a url")
            await self._page.goto(request.url, wait_until="load")
            # A frameset's children attach after the parent's load event. Without
            # settling here, an observation taken immediately afterwards sees
            # frames that report neither a name nor a URL yet.
            await self._settle()
            return ActionOutcome(ok=True, detail=f"navigated to {request.url}", navigated=True)

        if request.type == "wait_for":
            await self._page.wait_for_timeout(400)
            return ActionOutcome(ok=True, detail="waited")

        if request.ref is None:
            raise SurfaceError(f"{request.type} requires an element reference")

        locator = await self._locate(request.ref)

        if request.type == "click":
            before = self._page.url
            await locator.click(timeout=8000)
            await self._settle()
            return ActionOutcome(
                ok=True, detail="clicked", navigated=self._page.url != before
            )

        if request.type == "type_text":
            await locator.fill(request.text or "", timeout=8000)
            return ActionOutcome(ok=True, detail="typed")

        if request.type == "select_option":
            await locator.select_option(request.option or request.text or "", timeout=8000)
            return ActionOutcome(ok=True, detail="selected")

        if request.type == "press_key":
            before = self._page.url
            await locator.press(request.key or "Enter", timeout=8000)
            await self._settle()
            return ActionOutcome(
                ok=True, detail=f"pressed {request.key}", navigated=self._page.url != before
            )

        if request.type == "extract":
            value = await locator.input_value() if await self._is_input(locator) else None
            if value is None:
                value = (await locator.text_content()) or ""
            return ActionOutcome(ok=True, detail="extracted", extracted=value.strip())

        raise SurfaceError(f"unsupported action {request.type!r}")

    async def _is_input(self, locator) -> bool:
        try:
            tag = await locator.evaluate("el => el.tagName.toLowerCase()")
            return tag in {"input", "textarea", "select"}
        except PlaywrightError:
            return False

    async def _settle(self, quiet_ms: int = 250) -> None:
        """Let a full page load finish before the next observation.

        These applications navigate on every interaction, so the useful signal is
        the load event rather than network idle. A short quiet period afterwards
        absorbs the frameset's child loads.
        """
        try:
            await self._page.wait_for_load_state("load", timeout=8000)
        except PlaywrightError:
            pass
        await self._page.wait_for_timeout(quiet_ms)

    # -- evidence --------------------------------------------------------

    async def screenshot(self, path: str, mask_refs: list[str] | None = None) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        masks = []
        for ref in mask_refs or []:
            try:
                masks.append(await self._locate(ref))
            except SurfaceError:
                continue
        try:
            await self._page.screenshot(path=path, mask=masks or None, full_page=False)
        except PlaywrightError as exc:
            raise SurfaceError(f"screenshot failed: {exc}") from exc
        return path

    async def snapshot(self, path: str) -> str:
        """Persist every frame's markup plus the normalized element graph.

        Written only on failure. When a replay stops because a control was not
        where the recording said it would be, this is the artifact that answers
        "what was actually on the page" without re-running anything.
        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        chunks: list[str] = []
        for frame_path, frame in self._frames():
            label = "/".join(frame_path) or "main"
            try:
                content = await frame.content()
            except PlaywrightError:
                content = "<frame detached before snapshot>"
            chunks.append(f"<!-- frame: {label} url: {frame.url} -->\n{content}")
        Path(path).write_text("\n\n".join(chunks), encoding="utf-8")
        return path


async def launch_web_surface(headless: bool = False) -> WebSurface:
    return await WebSurface.launch(headless=headless)


__all__ = ["WebSurface", "launch_web_surface", "asyncio"]
