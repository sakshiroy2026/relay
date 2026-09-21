import json
from dataclasses import asdict, dataclass, field
from typing import Any

from relay.llm.client import Response, StopReason, ToolCall


@dataclass(frozen=True)
class ScriptedReply:
    """One pre-written model reply. Token counts are filled in by the fake."""

    content: str = ""
    stop_reason: StopReason = "end_turn"
    tool_calls: list[ToolCall] = field(default_factory=list)


def _estimate_tokens(obj: Any) -> int:
    """Rough rule of thumb: ~4 characters per token."""
    return max(1, len(json.dumps(obj, default=str)) // 4)


class FakeLLMClient:
    """Scripted, deterministic stand-in for a real model. Costs $0."""

    def __init__(self, script: list[ScriptedReply]) -> None:
        self._script = script

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Response:
        # Which reply? Decided by the conversation, not by a counter —
        # so a fresh worker after a crash picks the same reply the old one would have.
        turn = sum(1 for m in messages if m.get("role") == "assistant")
        if turn >= len(self._script):
            raise RuntimeError(
                f"fake script has {len(self._script)} replies; asked for reply #{turn + 1}"
            )
        reply = self._script[turn]

        return Response(
            model=model,
            content=reply.content,
            stop_reason=reply.stop_reason,
            input_tokens=_estimate_tokens([system, messages, tools]),
            output_tokens=_estimate_tokens([reply.content, [asdict(c) for c in reply.tool_calls]]),
            tool_calls=list(reply.tool_calls),
        )
