from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

StopReason = Literal["end_turn", "tool_use"]


@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked us to run."""

    id: str  # provider's id; the tool_result must echo it back
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Response:
    """One model reply, in Relay's own shape — the only shape the loop and ledger see."""

    model: str
    content: str  # the text part of the reply ("" if the model only called tools)
    stop_reason: StopReason
    input_tokens: int
    output_tokens: int
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient(Protocol):
    """Anything with this method can be plugged into the agent loop."""

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Response: ...
