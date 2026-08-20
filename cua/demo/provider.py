"""A scripted provider that reads the same prompt a real model reads.

The obvious way to script a run is to hand the orchestrator a fixed list of refs.
That would be a worse test, because refs change every observation and hard-coding
them would prove only that the harness can talk to itself.

Instead this parses the rendered observation out of the prompt -- exactly the
text a real model is given -- and resolves each scripted step's description of a
control ("the textbox labelled Member Number") against it. Two things fall out of
that. The scripted path exercises the real rendering rather than bypassing it, so
if the observation is not informative enough for a model to act on, the scripted
run breaks too. And the script reads as a description of the flow rather than a
list of opaque identifiers.
"""

from __future__ import annotations

import re
from typing import Any

from cua.demo.scripts import ScriptRunner, ScriptedAction
from cua.llm.base import LLMError, LLMResponse, Message
from cua.surfaces.base import Element, Observation

_LINE = re.compile(
    r"^\s*\[(?P<ref>[^\]]+)\]\s+(?P<role>\S+)"
    r"(?:\s+\"(?P<name>[^\"]*)\")?"
    r"(?P<rest>.*)$"
)
_LABEL = re.compile(r"label:'([^']*)'")
_SECTION = re.compile(r"in:'([^']*)'")
_FRAME = re.compile(r"frame:(\S+)")
_URL = re.compile(r"^URL:\s*(.+)$", re.MULTILINE)


def parse_observation(prompt: str) -> Observation:
    """Reconstruct the element graph from its rendered form."""
    elements: list[Element] = []
    in_controls = False

    for line in prompt.splitlines():
        if line.startswith("Controls on screen:"):
            in_controls = True
            continue
        if in_controls and (not line.strip() or line.startswith("Visible text:")):
            in_controls = False
            continue
        if not in_controls:
            continue

        match = _LINE.match(line)
        if not match:
            continue
        rest = match.group("rest") or ""
        label = _LABEL.search(rest)
        section = _SECTION.search(rest)
        frame = _FRAME.search(rest)

        elements.append(
            Element(
                ref=match.group("ref"),
                role=match.group("role"),
                name=match.group("name") or "",
                label_hint=label.group(1) if label else "",
                section=section.group(1) if section else "",
                frame_path=tuple(frame.group(1).split("/")) if frame else (),
                enabled="(disabled)" not in rest,
            )
        )

    url_match = _URL.search(prompt)
    return Observation(
        url=url_match.group(1).strip() if url_match else "", title="", elements=elements
    )


class ScriptedRunnerProvider:
    name = "scripted"

    def __init__(
        self, actions: list[ScriptedAction], inputs: dict[str, str], model: str = "scripted-0"
    ) -> None:
        self.model = model
        self._runner = ScriptRunner(actions, inputs)

    async def decide(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        prompt = messages[-1].content if messages else ""
        observation = parse_observation(prompt)
        try:
            call = self._runner.next_call(observation)
        except RuntimeError as exc:
            raise LLMError(str(exc)) from exc

        known = {tool["name"] for tool in tools}
        if call.name not in known:
            raise LLMError(f"scripted step calls unknown tool {call.name!r}")
        return LLMResponse(tool_call=call, stop_reason="tool_use")
