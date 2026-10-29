"""P1: the real-model adapter, through the real SDK, with a mock HTTP transport.

No request leaves the machine and no key is used: the SDK's own serializer,
error classes and headers run for real against httpx2.MockTransport. The
property that matters most for Relay is tested end to end: across a crash and
a takeover, every request of a run is an append-only extension of the one
before it, with each thinking block replayed byte-for-byte — exactly what the
API checks before it accepts a replayed thinking signature.
"""

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import anthropic
import httpx2
import pytest

import relay.llm.factory as factory
from relay.agent.fake_script import _FINAL_RECORD, ENRICH_SCRIPT
from relay.agent.loop import run_agent
from relay.core.retry import FatalError, RetryableError
from relay.llm.anthropic_client import AnthropicLLMClient, strip_thinking, to_api_messages
from relay.llm.client import Response
from relay.llm.pricing import UnknownModelPrice, assert_priced, price

URL = "https://api.anthropic.com/v1/messages"


# ── a scripted "Anthropic API" ───────────────────────────────────────────
def reply(content: list[dict[str, Any]], stop: str, **usage: int) -> dict[str, Any]:
    return {
        "id": f"msg_{uuid.uuid4().hex[:8]}",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 200, **usage},
    }


def scripted_turn(turn: int) -> dict[str, Any]:
    """The enrichment script, as a real model would send it: thinking + tool calls."""
    thinking = {"type": "thinking", "thinking": "", "signature": f"sig-turn-{turn}"}
    s = ENRICH_SCRIPT[turn]
    tool_uses = [
        {"type": "tool_use", "id": f"toolu_{tc.id}", "name": tc.name, "input": tc.args}
        for tc in s.tool_calls
    ]
    text = [{"type": "text", "text": s.content}] if s.content else []
    return reply([thinking, *text, *tool_uses], s.stop_reason, cache_read_input_tokens=500)


class FakeAPI:
    """Answers like the Messages API; records every request body it receives."""

    def __init__(self, answer: Callable[[dict[str, Any]], httpx2.Response] | None = None):
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.answer = answer or self.by_turn

    @staticmethod
    def by_turn(body: dict[str, Any]) -> httpx2.Response:
        turn = sum(1 for m in body["messages"] if m["role"] == "assistant")
        return httpx2.Response(200, json=scripted_turn(turn))

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        return self.answer(self.bodies[-1])

    def client(self) -> AnthropicLLMClient:
        sdk = anthropic.Anthropic(
            api_key="test-not-a-real-key",
            max_retries=0,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self.handler)),
        )
        return AnthropicLLMClient(client=sdk)


def status(code: int, message: str = "nope") -> Callable[[dict[str, Any]], httpx2.Response]:
    return lambda _b: httpx2.Response(
        code, json={"type": "error", "error": {"type": "x", "message": message}}
    )


CONVERSATION: list[dict[str, Any]] = [{"role": "user", "content": "Enrich acme.example"}]


# ── message conversion ───────────────────────────────────────────────────
def test_tool_results_of_one_turn_become_one_user_message() -> None:
    raw = [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "x"}]
    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "x", "tool_calls": [], "raw": raw},
        {"role": "tool", "tool_call_id": "a", "name": "t", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "name": "t", "content": "2"},
        {"role": "user", "content": "correction"},
    ]
    out = to_api_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user", "user"]
    assert out[1]["content"] == raw  # verbatim
    assert out[1]["content"] is not raw  # a copy: the ledger's data is never mutated
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["a", "b"]


def test_fake_turns_without_raw_blocks_are_built_plainly() -> None:
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "name": "n", "args": {}}]}
    ]
    assert to_api_messages(msgs)[0]["content"] == [
        {"type": "tool_use", "id": "c", "name": "n", "input": {}}
    ]  # no empty text block (the API rejects those)


def test_strip_thinking_keeps_text_and_tool_calls() -> None:
    api = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "s"},
                {"type": "redacted_thinking", "data": "x"},
                {"type": "tool_use", "id": "t", "name": "n", "input": {}},
            ],
        }
    ]
    assert [b["type"] for b in strip_thinking(api)[0]["content"]] == ["tool_use"]
    assert len(api[0]["content"]) == 3  # the original is untouched


# ── one call through the SDK ─────────────────────────────────────────────
def test_request_shape_and_response_mapping() -> None:
    api = FakeAPI()
    r = api.client().call("claude-haiku-5-5", "SYSTEM", CONVERSATION, [{"name": "x"}])
    body = api.bodies[0]
    assert body["model"] == "claude-haiku-5-5" and body["system"] == "SYSTEM"
    assert body["output_config"] == {"effort": "medium"}
    assert body["cache_control"] == {"type": "ephemeral"}  # P3: the stable prefix is cached
    for absent in ("temperature", "top_p", "top_k", "thinking"):  # 400s on current models
        assert absent not in body
    assert api.headers[0]["anthropic-version"] == "2026-10-21"
    assert "test-not-a-real-key" not in json.dumps(body)

    assert r.stop_reason == "tool_use" and r.tool_calls[0].name == "web_search"
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens) == (1000, 200, 500)
    assert r.raw_content is not None and r.raw_content[0]["signature"] == "sig-turn-0"


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (429, RetryableError),
        (529, RetryableError),  # overloaded
        (500, RetryableError),
        (408, RetryableError),
        (400, FatalError),
        (401, FatalError),
        (403, FatalError),
        (404, FatalError),
        (413, FatalError),
    ],
)
def test_errors_land_in_the_right_bucket(code: int, error: type[Exception]) -> None:
    api = FakeAPI(status(code))
    with pytest.raises(error):
        api.client().call("claude-haiku-5-5", "S", CONVERSATION, [])
    assert len(api.bodies) == 1  # SDK retries are off: Relay counts every attempt itself


def test_connection_errors_are_retryable() -> None:
    def boom(_body: dict[str, Any]) -> httpx2.Response:
        raise httpx2.ConnectError("unreachable")

    with pytest.raises(RetryableError):
        FakeAPI(boom).client().call("claude-haiku-5-5", "S", CONVERSATION, [])


def test_thinking_mismatch_strips_and_retries_once() -> None:
    calls: list[int] = []

    def answer(body: dict[str, Any]) -> httpx2.Response:
        calls.append(1)
        has_thinking = any(
            b.get("type") == "thinking"
            for m in body["messages"]
            if m["role"] == "assistant"
            for b in m["content"]
        )
        if has_thinking:
            return status(
                400,
                "Invalid `signature` in `thinking` block. The block is bound "
                "to a different conversation.",
            )(body)
        return httpx2.Response(200, json=reply([{"type": "text", "text": "ok"}], "end_turn"))

    raw = [{"type": "thinking", "thinking": "", "signature": "old"}, {"type": "text", "text": "a"}]
    history = [
        *CONVERSATION,
        {"role": "assistant", "content": "a", "tool_calls": [], "raw": raw},
        {"role": "user", "content": "next"},
    ]
    r = FakeAPI(answer).client().call("claude-haiku-5-5", "S", history, [])
    assert r.content == "ok" and len(calls) == 2


def test_refusal_and_max_tokens_come_back_as_billed_responses() -> None:
    def answer_with(stop: str) -> Callable[[dict[str, Any]], httpx2.Response]:
        return lambda _b: httpx2.Response(200, json=reply([], stop))

    for stop in ("refusal", "max_tokens"):
        api = FakeAPI(answer_with(stop))
        r = api.client().call("claude-haiku-5-5", "S", CONVERSATION, [])
        assert r.stop_reason == stop
        assert price(r) > 0  # it was billed, so the ledger must record it


# ── pricing ──────────────────────────────────────────────────────────────
def _resp(**kw: int) -> Response:
    return Response(model="claude-haiku-5-5", content="", stop_reason="end_turn", **kw)  # type: ignore[arg-type]


def test_haiku_pricing_with_cache_and_the_long_prompt_card() -> None:
    # a 1M-token prompt is over 100K, so the whole request moves to the long card ($0.50/MTok)
    assert price(_resp(input_tokens=1_000_000, output_tokens=0)) == pytest.approx(0.50)
    small = _resp(input_tokens=10_000, output_tokens=1_000, cache_read_tokens=50_000)
    assert price(small) == pytest.approx((10_000 * 0.10 + 50_000 * 0.01 + 1_000 * 0.50) / 1e6)
    write = _resp(input_tokens=0, output_tokens=0, cache_write_tokens=10_000)
    assert price(write) == pytest.approx(10_000 * 0.125 / 1e6)


def test_unpriced_models_fail_before_spending() -> None:
    assert_priced("claude-haiku-5-5")
    with pytest.raises(UnknownModelPrice):
        assert_priced("claude-made-up-9")


# ── the loop, end to end, with a crash in the middle ─────────────────────
class Crash(Exception):
    """Stands in for kill -9 at the top of a turn."""


@pytest.fixture
def real_model(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAPI]:
    api = FakeAPI()
    monkeypatch.setattr(factory.settings, "llm_provider", "anthropic")
    monkeypatch.setitem(factory._clients, "anthropic", api.client())
    yield api


def _assistant_blocks(body: dict[str, Any]) -> list[Any]:
    return [m["content"] for m in body["messages"] if m["role"] == "assistant"]


def test_every_request_extends_the_last_even_across_a_crash(
    parked_run: tuple[uuid.UUID, str], real_model: FakeAPI
) -> None:
    run_id, domain = parked_run
    beats = 0

    def dies_at_turn_2() -> bool:
        nonlocal beats
        beats += 1
        if beats == 3:  # worker A is killed at the top of turn 2
            raise Crash
        return True

    with pytest.raises(Crash):
        run_agent(run_id, domain, "worker-A", dies_at_turn_2, budget_usd=1.0)
    assert len(real_model.bodies) == 2  # turns 0 and 1 were paid for

    result = run_agent(run_id, domain, "worker-B", lambda: True, budget_usd=1.0)
    assert result is not None and result["name"] == _FINAL_RECORD["name"]
    bodies = real_model.bodies
    assert len(bodies) == 4  # turns 2 and 3 only: nothing was paid for twice

    for prev, nxt in zip(bodies, bodies[1:], strict=False):
        # append-only: the earlier request's messages are an exact prefix of the next one's
        assert nxt["messages"][: len(prev["messages"])] == prev["messages"]
        assert nxt["system"] == prev["system"] and nxt["tools"] == prev["tools"]

    # the thinking signatures written by worker A are what worker B sent back
    assert [blocks[0]["signature"] for blocks in _assistant_blocks(bodies[3])] == [
        "sig-turn-0",
        "sig-turn-1",
        "sig-turn-2",
    ]


def test_a_billed_refusal_is_recorded_then_fails_the_run(
    parked_run: tuple[uuid.UUID, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.check_run import load_steps

    api = FakeAPI(lambda _b: httpx2.Response(200, json=reply([], "refusal")))
    monkeypatch.setattr(factory.settings, "llm_provider", "anthropic")
    monkeypatch.setitem(factory._clients, "anthropic", api.client())
    run_id, domain = parked_run
    assert run_agent(run_id, domain, "w", lambda: True, budget_usd=1.0) is None
    steps = load_steps(str(run_id))
    assert [s["kind"] for s in steps] == ["run_started", "model_call", "error"]
    assert steps[1]["payload"]["cost_usd"] > 0  # the money spent is in the ledger
    assert steps[2]["payload"]["reason"] == "model_refused"

    # a worker that died between the two rows: the resumed run fails without a new call
    calls_before = len(api.bodies)
    from relay.db import pool

    with pool.connection() as conn:
        conn.execute("DELETE FROM steps WHERE run_id = %s AND step_index = 2", (run_id,))
    assert run_agent(run_id, domain, "w2", lambda: True, budget_usd=1.0) is None
    assert len(api.bodies) == calls_before  # nothing re-paid
    assert load_steps(str(run_id))[-1]["payload"]["reason"] == "model_refused"


def test_an_unpriced_model_fails_the_run_not_the_worker(
    parked_run: tuple[uuid.UUID, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model the budget can't price never gets called: the run ends, the worker lives."""
    from scripts.check_run import load_steps

    monkeypatch.setattr(factory.settings, "llm_provider", "anthropic")
    monkeypatch.setattr(factory.settings, "anthropic_model", "claude-not-priced")
    run_id, domain = parked_run
    assert run_agent(run_id, domain, "w", lambda: True, budget_usd=1.0) is None
    last = load_steps(str(run_id))[-1]
    assert last["kind"] == "error" and last["payload"]["reason"] == "model_misconfigured"
