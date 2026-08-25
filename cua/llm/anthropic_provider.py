"""Anthropic adapter -- the primary discovery driver.

Two things here are load-bearing rather than incidental.

**Prompt caching.** The system prompt and the tool definitions are identical on
every one of a run's twenty-odd turns. Marking the last tool as a cache
breakpoint means they are billed once rather than per step, which is most of the
cost of a discovery run.

**Strict tool use.** ``tool_choice`` requires a tool call, so the model cannot
answer a question about which button to press with a paragraph about which
button to press. A response with no tool call is an error the orchestrator
handles, not prose for someone to parse.
"""

from __future__ import annotations

from typing import Any

from cua.llm.base import LLMError, LLMResponse, Message, ToolCall, resolve_setting

DEFAULT_MODEL = "claude-opus-5"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMError("the anthropic package is not installed") from exc

        key = resolve_setting(api_key, "ANTHROPIC_API_KEY")
        if not key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set. Put it in .env, or run with "
                "--provider scripted to exercise the loop without a model."
            )
        self.model = resolve_setting(model, "ANTHROPIC_MODEL", DEFAULT_MODEL)
        self._client = AsyncAnthropic(api_key=key)

    async def decide(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        payload_tools: list[dict[str, Any]] = [
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["input_schema"],
            }
            for tool in tools
        ]
        # One breakpoint at the end of the tool list covers the system prompt and
        # every tool definition -- the entire static prefix of the request.
        if payload_tools:
            payload_tools[-1] = dict(
                payload_tools[-1], cache_control={"type": "ephemeral"}
            )

        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=payload_tools,
                tool_choice={"type": "any"},
                messages=[{"role": m.role, "content": m.content} for m in messages],
            )
        except Exception as exc:
            raise LLMError(f"anthropic call failed: {exc}") from exc

        call: ToolCall | None = None
        text_parts: list[str] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                args = dict(block.input or {})
                call = ToolCall(
                    name=block.name,
                    arguments=args,
                    reasoning=str(args.get("why", "")).strip(),
                    call_id=block.id,
                )

        usage = getattr(response, "usage", None)
        return LLMResponse(
            tool_call=call,
            text="\n".join(text_parts).strip(),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            stop_reason=getattr(response, "stop_reason", "") or "",
        )
