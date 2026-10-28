"""The real model: an adapter from Relay's LLMClient shape to Anthropic's Messages API.

WHAT IT IS  One class, AnthropicLLMClient, that turns Relay's conversation
            (user / assistant / tool messages rebuilt from the ledger) into a
            Messages API request through the official SDK, and the reply back
            into Relay's Response. The loop, the ledger, replay and the budget
            don't know which model they talk to.
GOES IN     call(model, system, messages, tools): the same four things the fake
            gets. An assistant message may carry `raw`: the provider's reply
            blocks as stored in the ledger; they are sent back VERBATIM.
COMES OUT   Response with the text, tool calls, a mapped stop reason, token
            counts split into uncached / cache read / cache write / output, and
            raw_content (every reply block, thinking signatures included).
TOUCHES     The network (api.anthropic.com) through the SDK. The key is read
            by the SDK from ANTHROPIC_API_KEY; this module never sees it.
FAILS WHEN  Transient trouble (429, 408/409, 5xx incl. 529 overloaded, timeouts,
            connection errors) -> RetryableError: the loop retries with backoff
            (the SDK's own retries are OFF so attempts are counted once, in the
            ledger). Everything else (400, 401, 403, 404, 413, 422) ->
            FatalError: the run ends with a `model_fatal` error step. One 400
            is special: a replayed thinking block whose conversation changed
            (e.g. a deploy changed the system prompt mid-run) -> strip every
            thinking block from the history and retry ONCE, the documented
            recovery. A refusal or a max_tokens stop is NOT an exception: the
            call was billed, so it comes back as a Response with that stop
            reason, the loop records its cost, then fails the run.

Why raw blocks: on current models a thinking block is valid only while the
conversation before it is byte-identical. Relay rebuilds the conversation from
the ledger on every turn and after every crash, so it must replay the exact
blocks the API returned — rebuilding them from text would be an edit.
"""

import copy
import logging
from typing import Any

import anthropic

from relay.config import settings
from relay.core.retry import FatalError, RetryableError
from relay.llm.client import Response, StopReason, ToolCall

log = logging.getLogger(__name__)

THINKING_MISMATCH = "bound to a different conversation"  # the 400's documented wording
RETRYABLE_STATUS = {408, 409, 429}  # plus every 5xx

_STOP: dict[str | None, StopReason] = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "refusal": "refusal",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
}


class _ThinkingMismatch(Exception):
    """Internal: the API refused a replayed thinking block (conversation changed)."""


# ── Relay conversation -> API messages ───────────────────────────────────
def to_api_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic: the same ledger always produces the same request bytes.

    Consecutive tool messages become ONE user message of tool_result blocks
    (the API wants every result of an assistant turn in the next user turn).
    """
    out: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    def flush() -> None:
        if results:
            out.append({"role": "user", "content": list(results)})
            results.clear()

    for m in messages:
        role = m["role"]
        if role == "tool":
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": m["content"] or "(empty result)",
                }
            )
            continue
        flush()
        if role == "assistant":
            raw = m.get("raw")
            if raw:
                blocks = copy.deepcopy(raw)  # verbatim, thinking signatures included
            else:  # a turn written by the fake model (no raw blocks): build plain blocks
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [
                    {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["args"]}
                    for tc in m.get("tool_calls") or []
                ]
            out.append({"role": "assistant", "content": blocks})
        elif role == "user":
            out.append({"role": "user", "content": m["content"]})
        else:
            raise ValueError(f"unknown message role {role!r}")
    flush()
    return out


def strip_thinking(api_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The one-time recovery: drop thinking blocks, keep each turn's text and tool calls."""
    stripped = copy.deepcopy(api_messages)
    for m in stripped:
        if m["role"] == "assistant" and isinstance(m["content"], list):
            m["content"] = [
                b for b in m["content"] if b.get("type") not in ("thinking", "redacted_thinking")
            ]
    return stripped


# ── API reply -> Relay Response ──────────────────────────────────────────
def to_response(requested_model: str, msg: Any) -> Response:
    text = "".join(b.text for b in msg.content if b.type == "text")
    tool_calls = [
        ToolCall(id=b.id, name=b.name, args=dict(b.input))
        for b in msg.content
        if b.type == "tool_use"
    ]
    stop = _STOP.get(msg.stop_reason, "other")
    if stop == "tool_use" and not tool_calls:
        stop = "other"  # never hand the loop a tool turn with nothing to run
    usage = msg.usage
    return Response(
        model=requested_model,  # prices are looked up by what we asked for and checked at boot
        content=text,
        stop_reason=stop,
        input_tokens=int(usage.input_tokens or 0),
        output_tokens=int(usage.output_tokens or 0),
        tool_calls=tool_calls,
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        # exactly what the SDK itself would send back (verified against its serializer)
        raw_content=[b.model_dump(exclude_none=True, mode="json") for b in msg.content],
        served_model=getattr(msg, "model", None),
    )


def _describe(exc: anthropic.APIStatusError) -> str:
    # the status and the API's own message; never request headers (they hold the key)
    return f"{exc.status_code}: {getattr(exc, 'message', '') or type(exc).__name__}"[:300]


# ── the client ───────────────────────────────────────────────────────────
class AnthropicLLMClient:
    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        # max_retries=0: Relay retries itself (Day 8), so every attempt is counted
        # once and recorded in the ledger instead of hidden inside the SDK
        self._client = client or anthropic.Anthropic(
            max_retries=0, timeout=settings.llm_timeout_seconds
        )

    def _create(self, params: dict[str, Any]) -> Any:
        try:
            return self._client.messages.create(**params)
        except anthropic.APIConnectionError as exc:  # includes APITimeoutError
            raise RetryableError(f"model: {type(exc).__name__}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code == 400 and THINKING_MISMATCH in str(exc):
                raise _ThinkingMismatch from exc
            if exc.status_code in RETRYABLE_STATUS or exc.status_code >= 500:
                raise RetryableError(f"model unavailable ({_describe(exc)})") from exc
            raise FatalError(f"model request rejected ({_describe(exc)})") from exc

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Response:
        api_messages = to_api_messages(messages)
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": settings.llm_max_tokens,
            "system": system,  # frozen per agent version: part of every thinking block's prefix
            "tools": tools,  # TOOL_SCHEMAS: built once at import, identical on every call
            "messages": api_messages,
            "output_config": {"effort": settings.llm_effort},
            # thinking is left unset: adaptive (the model's default), billed as output
        }
        if settings.llm_prompt_cache:
            # automatic caching of the longest stable prefix: each turn re-reads the
            # previous turn's prefix at ~0.1x instead of paying for it again
            params["cache_control"] = {"type": "ephemeral"}
        try:
            msg = self._create(params)
        except _ThinkingMismatch:
            log.warning(
                "model: replayed thinking blocks rejected; stripping them and retrying once"
            )
            params["messages"] = strip_thinking(api_messages)
            try:
                msg = self._create(params)
            except _ThinkingMismatch as exc:  # can't happen with no thinking blocks left
                raise FatalError("model: thinking-block mismatch persisted after strip") from exc
        return to_response(model, msg)
