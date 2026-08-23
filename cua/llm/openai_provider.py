"""OpenAI adapter.

Present to keep the provider seam honest -- an abstraction with a single
implementation is a guess, not a seam -- and to make the project's central claim
demonstrable rather than asserted: the capability catalog lets an OpenAI agent
invoke a capability that Claude discovered, which is only evidence of decoupling
if the two are genuinely different code paths.
"""

from __future__ import annotations

import json
import os
from typing import Any

from cua.llm.base import LLMError, LLMResponse, Message, ToolCall

DEFAULT_MODEL = "gpt-4o"


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMError("the openai package is not installed") from exc

        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise LLMError(
                "OPENAI_API_KEY is not set. Put it in .env, or run with "
                "--provider scripted to exercise the loop without a model."
            )
        self.model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL

        # Several providers -- Google's Gemini, Groq, OpenRouter, a local
        # Ollama -- expose an OpenAI-compatible chat-completions endpoint with
        # tool calling. Pointing this adapter at one is a base URL away, which
        # makes a free tier usable for a smoke test without a fourth adapter to
        # maintain. Tool-call fidelity varies between them, so a run that works
        # here is evidence the loop is sound, not that any model is.
        base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        self._client = AsyncOpenAI(api_key=key, base_url=base_url or None)

    async def decide(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        payload_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                },
            }
            for tool in tools
        ]

        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                tools=payload_tools,
                tool_choice="required",
                messages=[{"role": "system", "content": system}]
                + [{"role": m.role, "content": m.content} for m in messages],
            )
        except Exception as exc:
            raise LLMError(f"openai call failed: {exc}") from exc

        choice = response.choices[0]
        call: ToolCall | None = None
        if choice.message.tool_calls:
            raw = choice.message.tool_calls[0]
            try:
                args = json.loads(raw.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise LLMError(f"openai returned unparseable tool arguments: {exc}") from exc
            call = ToolCall(
                name=raw.function.name,
                arguments=args,
                reasoning=str(args.get("why", "")).strip(),
                call_id=raw.id,
            )

        usage = getattr(response, "usage", None)
        return LLMResponse(
            tool_call=call,
            text=(choice.message.content or "").strip(),
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            stop_reason=choice.finish_reason or "",
        )
