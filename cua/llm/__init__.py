"""Model providers behind one narrow protocol."""

from __future__ import annotations

from typing import Any

from cua.llm.base import LLMError, LLMProvider, LLMResponse, Message, ToolCall
from cua.llm.scripted import ScriptedProvider

__all__ = [
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "ScriptedProvider",
    "ToolCall",
    "build_provider",
]


def build_provider(name: str, model: str | None = None, **kwargs: Any) -> LLMProvider:
    """Resolve a provider by name.

    SDKs are imported lazily so that neither vendor's package -- nor its key --
    is required to use the other, or to run the scripted path with neither.
    """
    key = (name or "anthropic").lower()
    if key == "anthropic":
        from cua.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=model)
    if key == "openai":
        from cua.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(model=model)
    if key == "scripted":
        return ScriptedProvider(script=kwargs.get("script", []))
    raise LLMError(f"unknown provider {name!r}; expected anthropic, openai, or scripted")
