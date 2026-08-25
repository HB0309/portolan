"""OpenAI adapter.

Present to keep the provider seam honest -- an abstraction with a single
implementation is a guess, not a seam -- and to make the project's central claim
demonstrable rather than asserted: the capability catalog lets an OpenAI agent
invoke a capability that Claude discovered, which is only evidence of decoupling
if the two are genuinely different code paths.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from cua.llm.base import LLMError, LLMResponse, Message, ToolCall, resolve_setting

DEFAULT_MODEL = "gpt-4o"

#: How many times to retry a 429 before giving up. A rate limit is exactly the
#: kind of transient condition retries exist for, unlike a silently hung
#: connection (see the timeout note on the client below) -- the server is
#: telling us precisely how long to wait.
_MAX_RATE_LIMIT_RETRIES = 4

#: Longest cooldown this will wait out silently before raising instead. Chosen
#: to comfortably cover a per-minute budget's typical wait (single-digit
#: seconds) while refusing a per-day budget's (minutes), where blocking
#: without telling anyone is indistinguishable from the hang this file's
#: client timeout exists to prevent.
_MAX_SILENT_WAIT_SECONDS = 45.0


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

        key = resolve_setting(api_key, "OPENAI_API_KEY")
        if not key:
            raise LLMError(
                "OPENAI_API_KEY is not set. Put it in .env, or run with "
                "--provider scripted to exercise the loop without a model."
            )
        self.model = resolve_setting(model, "OPENAI_MODEL", DEFAULT_MODEL)

        # Several providers -- Google's Gemini, Groq, OpenRouter, a local
        # Ollama -- expose an OpenAI-compatible chat-completions endpoint with
        # tool calling. Pointing this adapter at one is a base URL away, which
        # makes a free tier usable for a smoke test without a fourth adapter to
        # maintain. Tool-call fidelity varies between them, so a run that works
        # here is evidence the loop is sound, not that any model is.
        base_url = resolve_setting(base_url, "OPENAI_BASE_URL")
        # Some OpenAI-compatible endpoints (reproduced against NVIDIA's NIM
        # catalog while building this) accept a tool-calling request and then
        # go silent -- no response, no error, no stream event -- rather than
        # closing the connection or returning an error. The SDK's default
        # max_retries=2 means an unbounded per-request timeout compounds into
        # minutes of silence (timeout x (retries + 1)) before anything
        # surfaces, which stalls the whole discovery loop with no signal.
        # 20s is generous for a real response; a genuine hang then fails in
        # under a minute instead of after several.
        self._client = AsyncOpenAI(
            api_key=key, base_url=base_url or None, timeout=20.0, max_retries=1
        )

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

        response = await self._create_with_rate_limit_retry(
            payload_tools, system, messages, max_tokens
        )

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

    async def _create_with_rate_limit_retry(
        self,
        payload_tools: list[dict[str, Any]],
        system: str,
        messages: list[Message],
        max_tokens: int,
    ) -> Any:
        """Retry a 429 for as long as the server says it's worth retrying.

        Found running a real discovery loop against Groq's free tier: it
        answers in well under a second, so a ten-step loop bursts through an
        8,000-token-per-minute budget in seconds, and the SDK's own retry
        (``max_retries``, set low elsewhere in this file to avoid a different
        failure -- a silently hung connection compounding into minutes of
        silence) does not wait long enough for the cooldown the server itself
        names in the error. A rate limit is exactly the transient condition
        retries exist for -- unlike a hang, the server is telling us precisely
        how long to wait -- so this reads that number and honours it, up to a
        bounded number of attempts, before giving up loudly.

        Found a second time, live, on the same free tier: a *daily* token
        budget being exhausted from a day of testing looks identical to this
        at the type level -- still a 429, still a "try again in ..." message --
        but names its cooldown in minutes ("3m14.832s") rather than seconds.
        Silently, because nothing here printed anything while waiting, this
        read as a plain hang rather than a rate limit, and the fallback
        default (8s, sized for the per-minute case) meant retrying straight
        back into the same wall four times over before finally raising.
        Logging every wait is what turns "the run just froze" back into "the
        run is rate limited, here's why and for how long." It is not the whole
        fix, though: a print reaches a terminal someone is watching, not an
        operator standing at the console during an attended run with no view
        of this process's stdout. A per-minute budget clears in single-digit
        seconds either way, so waiting it out is invisible in practice. A
        per-day budget's cooldown can run to minutes, and blocking silently
        for that long -- logged or not -- reads the same as the hang this was
        built to avoid in the first place. Past ``_MAX_SILENT_WAIT_SECONDS``
        this gives up loudly instead of blocking, on the theory that a clear,
        immediate failure the operator can act on (wait and retry the run,
        switch providers) beats a console that looks frozen for minutes at a
        time regardless of what a log file says.
        """
        from openai import RateLimitError

        last_exc: Exception | None = None
        for attempt in range(_MAX_RATE_LIMIT_RETRIES):
            try:
                return await self._client.chat.completions.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    tools=payload_tools,
                    tool_choice="required",
                    messages=[{"role": "system", "content": system}]
                    + [{"role": m.role, "content": m.content} for m in messages],
                )
            except RateLimitError as exc:
                last_exc = exc
                wait_s = _retry_after_seconds(exc, default=8.0)
                if wait_s > _MAX_SILENT_WAIT_SECONDS:
                    raise LLMError(
                        f"openai call failed: rate limited, and the server's "
                        f"suggested cooldown ({wait_s:.0f}s) is too long to wait "
                        f"silently. This usually means a daily token quota is "
                        f"exhausted, not a per-minute limit -- wait for it to "
                        f"refill, or switch providers. Original error: {exc}"
                    ) from exc
                if attempt < _MAX_RATE_LIMIT_RETRIES - 1:
                    print(
                        f"  rate limited (attempt {attempt + 1}/"
                        f"{_MAX_RATE_LIMIT_RETRIES}); waiting {wait_s:.0f}s: "
                        f"{exc}",
                        flush=True,
                    )
                    await asyncio.sleep(wait_s)
                    continue
            except Exception as exc:
                raise LLMError(f"openai call failed: {exc}") from exc

        raise LLMError(
            f"openai call failed: rate limited after "
            f"{_MAX_RATE_LIMIT_RETRIES} attempts: {last_exc}"
        ) from last_exc


_RETRY_HINT = re.compile(
    r"try again in\s+(?:(?P<hours>[\d.]+)h)?(?:(?P<minutes>[\d.]+)m)?"
    r"(?:(?P<seconds>[\d.]+)s)?",
    re.IGNORECASE,
)


def _retry_after_seconds(exc: Exception, *, default: float) -> float:
    """Pull the server's suggested cooldown out of a 429, if it gave one.

    Checked in two places, because different providers put it in different
    ones. A standard ``Retry-After`` header if present; otherwise Groq's own
    convention of naming the wait inside the JSON error message's text.

    Written first against a per-minute limit's "Please try again in 5.69s",
    then found broken a second time against a per-day limit's "Please try
    again in 3m14.832s" -- same convention, a coarser unit once the wait is
    long enough to need one. A regex that only recognised bare seconds missed
    that entirely and silently fell back to the 8s default, which is far too
    short for a quota that will not refill for minutes: the caller kept
    retrying straight back into the same wall. Matching optional hour and
    minute components, not just seconds, is what makes the fallback default
    only apply when the server genuinely gave no hint at all.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        for key in ("retry-after", "Retry-After"):
            value = headers.get(key)
            if value:
                try:
                    return max(float(value), 0.5) + 0.5  # a small margin
                except ValueError:
                    pass

    match = _RETRY_HINT.search(str(exc))
    if match and any(match.group(name) for name in ("hours", "minutes", "seconds")):
        total = (
            float(match.group("hours") or 0) * 3600
            + float(match.group("minutes") or 0) * 60
            + float(match.group("seconds") or 0)
        )
        return total + 0.5

    return default
