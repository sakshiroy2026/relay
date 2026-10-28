import json
import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from relay.core.retry import RetryableError
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

    def __init__(
        self,
        script: list[ScriptedReply],
        delay_seconds: float = 0.0,
        failure_rate: float = 0.0,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._script = script
        self._delay_seconds = delay_seconds  # stand-in for real model latency
        self._failure_rate = failure_rate  # Day 8 proofs: chance of an injected 503
        self._rng = rng

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Response:
        if self._delay_seconds > 0:
            time.sleep(self._delay_seconds)

        # The injected failure comes AFTER the latency, like a real 503 would.
        if self._failure_rate > 0 and self._rng() < self._failure_rate:
            raise RetryableError("fake model: injected 503 (overloaded)")

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
