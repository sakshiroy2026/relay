from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

# end_turn / tool_use drive the loop. refusal / max_tokens / other come only from a
# real provider: the call was billed, so the loop records it, then fails the run.
StopReason = Literal["end_turn", "tool_use", "refusal", "max_tokens", "other"]


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
    input_tokens: int  # uncached input tokens (the provider's `input_tokens`)
    output_tokens: int  # includes thinking tokens: they are billed as output
    tool_calls: list[ToolCall] = field(default_factory=list)
    # ── real providers (P1); the fake leaves these at their defaults ──
    cache_read_tokens: int = 0  # prompt tokens served from the prompt cache (~0.1x price)
    cache_write_tokens: int = 0  # prompt tokens written to the cache (~1.25x price)
    # The provider's reply blocks exactly as returned (thinking blocks with their
    # signatures included). Replay sends these back verbatim: thinking blocks are
    # only valid in an unchanged conversation, so rebuilding them is not allowed.
    raw_content: list[dict[str, Any]] | None = None
    served_model: str | None = None  # what the provider says actually ran


class LLMClient(Protocol):
    """Anything with this method can be plugged into the agent loop."""

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Response: ...
