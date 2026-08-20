"""The model provider seam.

Narrow on purpose: one method that takes a system prompt, a conversation, and a
set of tool definitions, and returns exactly one typed tool call. Everything
provider-specific -- message shapes, cache directives, tool-call encodings --
stops at this boundary.

Two reasons the seam is worth having rather than calling one SDK directly.
Model portability is a genuine procurement requirement for regulated buyers, who
will not accept a system welded to a single vendor. And it makes the project's
central claim demonstrable rather than asserted: a capability discovered by one
vendor's model can be invoked by a different vendor's agent, which is only
interesting if the two are actually different code paths.

The agent returns a *tool call*, never prose to be parsed. A model that wants to
click a button has to say so in a shape the executor can validate, and anything
that fails validation is a refusal rather than a best-effort guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    #: The model's stated reason for this action. Carried into the step's
    #: ``intent`` so a human can audit the capability without replaying it.
    reasoning: str = ""
    call_id: str = ""


@dataclass
class LLMResponse:
    tool_call: ToolCall | None = None
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""

    @property
    def usable(self) -> bool:
        return self.tool_call is not None


@dataclass
class Message:
    """One turn. ``content`` is plain text; tool results are rendered into it.

    Flattening tool results into text rather than modelling each provider's
    tool-result envelope keeps the adapters trivial and costs nothing here: the
    agent only ever needs to know what it did and what the screen looks like now.
    """

    role: str
    content: str


class LLMError(RuntimeError):
    """The provider failed, refused, or returned something unusable."""


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str

    async def decide(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        ...
