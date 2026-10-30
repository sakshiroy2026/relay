"""The free-tier real model: an adapter from Relay's LLMClient shape to Gemini.

WHAT IT IS  One class, GeminiLLMClient, that turns Relay's conversation (user /
            assistant / tool messages rebuilt from the ledger) into a
            `generateContent` request on Google's Generative Language API, and
            the reply back into Relay's Response. Same contract as
            anthropic_client.py: the loop, the ledger, replay and the budget
            don't know which provider they talk to.
GOES IN     call(model, system, messages, tools): the same four things the fake
            gets. An assistant message may carry `raw`: the provider's reply
            parts as stored in the ledger; they are sent back VERBATIM.
COMES OUT   Response with the answer text (thought parts excluded), tool calls,
            a mapped stop reason, token counts split into uncached / cache read
            / output (thinking counted as output, which is how Google bills it),
            and raw_content: every reply part, `thoughtSignature`s included.
TOUCHES     The network (generativelanguage.googleapis.com) over httpx2 — the
            HTTP client the Anthropic SDK already pulls in, so no new
            dependency. The key is read once from GEMINI_API_KEY and sent as the
            `x-goog-api-key` header: never in the URL, never logged.
FAILS WHEN  No GEMINI_API_KEY -> RuntimeError at construction, before any call
            (the loop turns that into a `model_misconfigured` error step).
            Transient trouble (408, 429 with a per-minute quota, 5xx, timeouts,
            connection errors) -> RetryableError: the loop retries with backoff.
            A 429 whose quota is the DAILY free-tier allowance -> FatalError:
            no amount of backoff brings it back today, so it must not burn
            every retry. Everything else (400, 401, 403, 404) -> FatalError and
            a `model_fatal` error step. A refusal or a max-tokens stop is NOT
            an exception: the call was metered, so it comes back as a Response
            with that stop reason, the loop records it, then fails the run.

Why raw parts: Gemini returns a `thoughtSignature` on the parts of a thinking
turn, and those signatures are only valid inside the conversation that produced
them — they must be replayed unchanged on every later turn. Relay rebuilds the
conversation from the ledger on every turn and after every crash, so it stores
the reply's parts verbatim and sends them straight back. Rebuilding them from
text would be an edit, and the run would stop being resumable. This is the same
trick the Anthropic adapter plays with thinking blocks.

Why thinking config is opt-in: `thinkingLevel` / `thinkingBudget` are spelled
differently across model generations, so by default nothing is sent and each
model uses its own default. GEMINI_THINKING_LEVEL (P3) sends `thinkingLevel` to
the 3.x models only. Thinking tokens are billed as output either way, and
`gemini_max_output_tokens` leaves room for them inside one reply.
"""

import copy
import json
import logging
import os
from typing import Any

import httpx2

from relay.config import settings
from relay.core.retry import FatalError, RetryableError
from relay.llm.client import Response, StopReason, ToolCall

log = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
RETRYABLE_STATUS = {408, 429}  # plus every 5xx; a daily-quota 429 is pulled back out
SIGNATURE_TROUBLE = "thought signature"  # the 400's wording when a replayed signature is refused
# A free-tier 429 names the quota it broke. The per-DAY ones never clear with
# backoff, so they are fatal; the per-minute ones are exactly what retries are for.
DAILY_QUOTA_MARKERS = ("perday", "per day", "requestsperday")
SYNTHETIC_ID_PREFIX = "relayfc-"  # marks a tool-call id Relay minted, not Gemini
THINKING_LEVEL_MODELS = ("gemini-3",)  # the generation that spells it `thinkingLevel`

# finishReason -> Relay's stop reason. A reply with a functionCall in it is a
# tool turn whatever the finish reason says, so that case is handled first.
_STOP: dict[str, StopReason] = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "refusal",
    "RECITATION": "refusal",
    "BLOCKLIST": "refusal",
    "PROHIBITED_CONTENT": "refusal",
    "SPII": "refusal",
    "IMAGE_SAFETY": "refusal",
}


class _SignatureTrouble(Exception):
    """Internal: the API refused a replayed thought signature (conversation changed)."""


# ── Relay conversation -> API contents ───────────────────────────────────
def _function_response_part(name: str, content: str, call_id: str) -> dict[str, Any]:
    # `response` must be an object. The tool's own text goes in one field,
    # unparsed: re-encoding the JSON our tools return would make the request
    # bytes depend on Python's serializer instead of on the ledger.
    part: dict[str, Any] = {
        "functionResponse": {"name": name, "response": {"content": content or "(empty result)"}}
    }
    if not call_id.startswith(SYNTHETIC_ID_PREFIX):
        # Gemini only gets an id back if Gemini gave us one; our own stand-in
        # ids (see synthetic_call_id) would be rejected as unknown.
        part["functionResponse"]["id"] = call_id
    return part


def to_api_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic: the same ledger always produces the same request bytes.

    Consecutive tool messages become ONE user content of functionResponse parts
    (the API wants every result of a model turn in the next user turn).
    """
    out: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    def flush() -> None:
        if results:
            out.append({"role": "user", "parts": list(results)})
            results.clear()

    for m in messages:
        role = m["role"]
        if role == "tool":
            results.append(_function_response_part(m["name"], m["content"], m["tool_call_id"]))
            continue
        flush()
        if role == "assistant":
            raw = m.get("raw")
            if raw:
                parts = copy.deepcopy(raw)  # verbatim, thought signatures included
            else:  # a turn written by the fake model (no raw parts): build plain ones
                parts = [{"text": m["content"]}] if m.get("content") else []
                parts += [
                    {"functionCall": {"name": tc["name"], "args": tc["args"]}}
                    for tc in m.get("tool_calls") or []
                ]
            out.append({"role": "model", "parts": parts})
        elif role == "user":
            out.append({"role": "user", "parts": [{"text": m["content"]}]})
        else:
            raise ValueError(f"unknown message role {role!r}")
    flush()
    return out


def strip_signatures(contents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The one-time recovery: drop thought parts and signatures, keep text and calls."""
    stripped = copy.deepcopy(contents)
    for content in stripped:
        if content["role"] != "model":
            continue
        kept = []
        for part in content["parts"]:
            if part.get("thought"):
                continue  # the model's private reasoning: not part of the transcript
            part.pop("thoughtSignature", None)
            kept.append(part)
        content["parts"] = kept
    return stripped


# ── Relay tool schemas -> functionDeclarations ───────────────────────────
def _without_titles(node: Any) -> Any:
    """Pydantic puts a `title` on every field; it is noise the model pays for."""
    if isinstance(node, dict):
        return {k: _without_titles(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_without_titles(v) for v in node]
    return node


def to_function_declarations(tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """TOOL_SCHEMAS -> Gemini declarations, via `parametersJsonSchema`.

    `parametersJsonSchema` takes real JSON Schema, so Pydantic's output goes
    through as-is: the `anyOf: [T, null]` of an Optional field, the
    `additionalProperties` of `dict[str, float]`, `format: uri`, and the
    min/max bounds all survive. The older `parameters` field is an OpenAPI 3.0
    subset that drops several of those, which would let the model send args the
    Pydantic models then reject.
    """
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "parametersJsonSchema": _without_titles(tool["input_schema"]),
        }
        for tool in tool_schemas
    ]


# ── API reply -> Relay Response ──────────────────────────────────────────
def synthetic_call_id(turn: int, position: int) -> str:
    """A stand-in id for a functionCall Gemini did not give one.

    Deterministic from where the call sits in the conversation, so a replay of
    the same ledger would mint the same id. It becomes part of the tool's
    idempotency key ('<run_id>:<id>'), which is why it may never be random.
    """
    return f"{SYNTHETIC_ID_PREFIX}{turn}-{position}"


def usage_tokens(body: dict[str, Any]) -> tuple[int, int, int]:
    """(uncached input, cache reads, output). Google bills thinking as output."""
    usage = body.get("usageMetadata") or {}
    prompt = int(usage.get("promptTokenCount") or 0)
    cached = int(usage.get("cachedContentTokenCount") or 0)
    output = int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)
    return max(prompt - cached, 0), cached, output


def to_response(requested_model: str, body: dict[str, Any], turn: int) -> Response:
    candidates = body.get("candidates") or []
    candidate = candidates[0] if candidates else {}
    parts = (candidate.get("content") or {}).get("parts") or []

    # thought parts are the model's private reasoning: replayed, never read as the answer
    text = "".join(p.get("text") or "" for p in parts if not p.get("thought"))
    tool_calls = [
        ToolCall(
            id=call.get("id") or synthetic_call_id(turn, i),
            name=call.get("name") or "",
            args=dict(call.get("args") or {}),
        )
        for i, p in enumerate(parts)
        if (call := p.get("functionCall")) is not None
    ]

    finish = str(candidate.get("finishReason") or "")
    if tool_calls:
        stop: StopReason = "tool_use"
    elif not candidates and (body.get("promptFeedback") or {}).get("blockReason"):
        stop = "refusal"  # the prompt itself was blocked: metered, nothing to act on
    else:
        stop = _STOP.get(finish, "other")

    uncached, cache_read, output = usage_tokens(body)
    return Response(
        model=requested_model,  # prices are looked up by what we asked for, checked at boot
        content=text,
        stop_reason=stop,
        input_tokens=uncached,
        output_tokens=output,
        tool_calls=tool_calls,
        cache_read_tokens=cache_read,
        # implicit caching has no write charge, and Relay writes no explicit caches
        cache_write_tokens=0,
        raw_content=copy.deepcopy(parts),  # verbatim: thought signatures ride along
        served_model=body.get("modelVersion") or None,
    )


# ── errors ───────────────────────────────────────────────────────────────
def _error_text(response: httpx2.Response) -> str:
    """The API's own message, never a header: headers carry the key."""
    try:
        detail = response.json().get("error") or {}
        message = str(detail.get("message") or "")
        status = str(detail.get("status") or "")
    except Exception:
        message, status = "", ""
    return f"{response.status_code}: {status or response.reason_phrase} {message}".strip()[:300]


def is_daily_quota(response: httpx2.Response) -> bool:
    """True when a 429 broke a per-DAY quota, which backing off cannot fix."""
    try:
        body = response.json()
    except Exception:
        return False
    haystack = json.dumps(body).lower().replace("_", "").replace("-", "")
    return any(marker.replace(" ", "") in haystack for marker in DAILY_QUOTA_MARKERS)


def generation_config(model: str) -> dict[str, Any]:
    """The per-call settings. With no level set, exactly what P7 always sent."""
    config: dict[str, Any] = {"maxOutputTokens": settings.gemini_max_output_tokens}
    if settings.gemini_thinking_level and model.startswith(THINKING_LEVEL_MODELS):
        config["thinkingConfig"] = {"thinkingLevel": settings.gemini_thinking_level}
    return config


# ── the client ───────────────────────────────────────────────────────────
class GeminiLLMClient:
    def __init__(self, client: httpx2.Client | None = None, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("GEMINI_API_KEY") or ""
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set: this worker cannot run Gemini runs")
        # One client per process: it keeps a connection pool, like the SDK does.
        # Relay retries itself (Day 8), so every attempt is counted once in the ledger.
        self._client = client or httpx2.Client(timeout=settings.llm_timeout_seconds)
        self._headers = {"x-goog-api-key": key, "Content-Type": "application/json"}

    def generate(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One generateContent request, errors mapped to Retryable / Fatal."""
        url = f"{API_ROOT}/models/{model}:generateContent"
        try:
            response = self._client.post(url, json=payload, headers=self._headers)
        except httpx2.TimeoutException as exc:
            raise RetryableError("model: timeout") from exc
        except httpx2.RequestError as exc:
            raise RetryableError(f"model: {type(exc).__name__}") from exc

        if response.status_code == 200:
            parsed: dict[str, Any] = response.json()
            return parsed
        if response.status_code == 400 and SIGNATURE_TROUBLE in response.text.lower():
            raise _SignatureTrouble
        if response.status_code == 429 and is_daily_quota(response):
            # the free tier's daily allowance is gone until midnight Pacific:
            # fatal on purpose, so the run ends honestly instead of retrying
            raise FatalError(f"model daily quota exhausted ({_error_text(response)})")
        if response.status_code in RETRYABLE_STATUS or response.status_code >= 500:
            raise RetryableError(f"model unavailable ({_error_text(response)})")
        raise FatalError(f"model request rejected ({_error_text(response)})")

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Response:
        contents = to_api_contents(messages)
        turn = sum(1 for c in contents if c["role"] == "model")  # which model turn this will be
        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "tools": [{"functionDeclarations": to_function_declarations(tools)}],
            "generationConfig": generation_config(model),
        }
        try:
            body = self.generate(model, payload)
        except _SignatureTrouble:
            log.warning(
                "model: replayed thought signatures rejected; stripping them and retrying once"
            )
            payload["contents"] = strip_signatures(contents)
            try:
                body = self.generate(model, payload)
            except _SignatureTrouble as exc:  # can't happen with no signatures left
                raise FatalError("model: thought-signature mismatch persisted after strip") from exc
        return to_response(model, body, turn)
