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

        # Each frame's extraction is an independent CDP round trip with no
        # dependency on any other frame's result. observe() is called at
        # least twice per replay step and once per discovery step, so on a
        # frameset app this was N sequential round trips every single step of
        # the hot loop; gathering them gets it down to the slowest one. Frame
        # order (not completion order) still decides ref numbering below --
        # asyncio.gather preserves the input list's order in its results.
        frames = self._frames()

        async def extract(frame: Frame) -> Any:
            try:
                return await frame.evaluate(_EXTRACT_JS)
            except PlaywrightError:
                # A frame can detach between enumeration and evaluation during
                # a navigation. That is normal, not a failure of perception.
                return None

        payloads = await asyncio.gather(*(extract(frame) for _, frame in frames))

        for frame_index, ((path, _frame), payload) in enumerate(zip(frames, payloads)):
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
                        row_peers=tuple(raw.get("row_peers", []) or []),
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

    async def settle(self) -> None:
        await self._settle()

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

        frame = frames[frame_index][1]
        locator = frame.locator(f"[data-cua-ref='{local_ref}']")
        if await locator.count() == 0:
            raise SurfaceError(f"element {element_ref} is no longer on the page")
        return locator.first, frame

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

        locator, frame = await self._locate(request.ref)

        if request.type == "click":
            return await self._click_like(
                request, lambda: locator.click(timeout=8000), frame, "clicked"
            )

        if request.type == "type_text":
            await locator.fill(request.text or "", timeout=8000)
            return ActionOutcome(ok=True, detail="typed")

        if request.type == "select_option":
            await locator.select_option(request.option or request.text or "", timeout=8000)
            return ActionOutcome(ok=True, detail="selected")

        if request.type == "press_key":
            return await self._click_like(
                request,
                lambda: locator.press(request.key or "Enter", timeout=8000),
                frame,
                f"pressed {request.key}",
            )

        if request.type == "extract":
            value = await locator.input_value() if await self._is_input(locator) else None
            if value is None:
                value = (await locator.text_content()) or ""
            return ActionOutcome(ok=True, detail="extracted", extracted=value.strip())

        raise SurfaceError(f"unsupported action {request.type!r}")

    async def _click_like(self, request: ActionRequest, do, frame: Frame, detail: str):
        """Perform an action that may navigate, and wait for it correctly.

        The waiter has to be armed *before* the action. Immediately after a click
        the frame still holds the old, fully loaded document, so anything that
        asks "is it loaded yet" is answered by the page we just clicked away
        from — and the next observation asserts against the previous screen. On a
        slow server that surfaces as a checkpoint violation for a page that was
        merely still on its way.

        Whether to expect a navigation comes from the recorded step, which knows
        because the discovery run watched it happen. Guessing instead would mean
        either waiting the full navigation budget after every click that does not
        navigate, or racing the ones that do.
        """
        before = self._page.url
        before_frame = frame.url

        if request.expect_navigation:
            try:
                async with frame.expect_navigation(
                    timeout=request.navigation_timeout_ms, wait_until="load"
                ):
                    await do()
            except PlaywrightError:
                # The step was recorded as navigating but did not this time --
                # a validation error kept us on the page, say. That is a real
                # state the caller needs to see, so it is reported by the
                # observation rather than raised here.
                pass
        else:
            await do()

        await self._settle()
        navigated = self._page.url != before or frame.url != before_frame
        return ActionOutcome(ok=True, detail=detail, navigated=navigated)

    async def _is_input(self, locator) -> bool:
        try:
            tag = await locator.evaluate("el => el.tagName.toLowerCase()")
            return tag in {"input", "textarea", "select"}
        except PlaywrightError:
            return False

    async def _settle(self, quiet_ms: int = 250, timeout_ms: int = 15_000) -> None:
        """Let a navigation finish before the next observation.

        Two things make this harder than it looks on a frameset application.

        Waiting on the *page* is not enough: clicking a control in the content
        frame navigates that frame while the top-level frameset document stays
        loaded, so `page.wait_for_load_state("load")` is satisfied by a document
        that never changed.

        And waiting on the frame is not enough either, because immediately after
        a click the new request has not been issued yet -- the frame still holds
        the *old*, fully loaded document, so the wait returns instantly and the
        next observation sees the page we just clicked away from. That is what
        turned a slow response into a checkpoint violation: the automation was
        asserting against the previous screen.

        So the wait is for network quiet first, which is what actually tracks the
        in-flight navigation, and only then for each frame's load state.
        """
        try:
            await self._page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightError:
            # networkidle can time out on a page that polls. The per-frame wait
            # below still applies, and the observation reports what is there.
            pass

        # Each frame's wait is independent of every other frame's, so waiting
        # on them one at a time serialized their timeouts instead of being
        # bounded by the slowest one -- and _settle() runs after essentially
        # every action in both discovery and replay.
        async def wait_for_frame(frame: Frame) -> None:
            try:
                await frame.wait_for_load_state("load", timeout=timeout_ms)
            except PlaywrightError:
                pass

        await asyncio.gather(*(wait_for_frame(frame) for frame in self._page.frames))
        await self._page.wait_for_timeout(quiet_ms)

    # -- evidence --------------------------------------------------------

    async def screenshot(self, path: str, mask_refs: list[str] | None = None) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        masks = []
        for ref in mask_refs or []:
            try:
                located, _ = await self._locate(ref)
                masks.append(located)
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


__all__ = ["WebSurface"]
