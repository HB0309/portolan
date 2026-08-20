"""A provider that replays a fixed sequence of tool calls.

It exists for two practical reasons, and is not pretending to a third.

The discovery loop, the policy gate, the recorder and the artifact emission all
need to be testable deterministically, in CI, with no key and no network. And a
reviewer without an API key needs a way to run the demo path end to end, which
.

What it does **not** do is show that the agent can work anything out. A scripted
run proves the plumbing; only a real model-driven run proves discovery, and the
committed evidence contains one. Every place the scripted path can be selected
says so, so nothing is overclaimed by accident.
"""

from __future__ import annotations

from typing import Any

from cua.llm.base import LLMError, LLMResponse, Message, ToolCall


class ScriptedProvider:
    name = "scripted"

    def __init__(self, script: list[ToolCall], model: str = "scripted-0") -> None:
        self.model = model
        self._script = list(script)
        self._index = 0

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self._script)

    async def decide(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        if self.exhausted:
            raise LLMError("scripted provider ran out of steps before the goal was reached")
        call = self._script[self._index]
        self._index += 1

        known = {tool["name"] for tool in tools}
        if call.name not in known:
            raise LLMError(f"scripted step {self._index} calls unknown tool {call.name!r}")

        return LLMResponse(tool_call=call, text="", stop_reason="tool_use")
