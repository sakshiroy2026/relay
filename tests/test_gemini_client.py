"""P7: the free-tier model adapter, driven by a mock HTTP transport.

No request leaves the machine and no key is used: the adapter's own request
building, error mapping and header handling run for real against
httpx2.MockTransport. The property that matters most for Relay is tested end to
end: across a crash and a takeover, every request of a run is an append-only
extension of the one before it, with each thought signature replayed
byte-for-byte — which is exactly what Gemini checks before it accepts one.

Also here, because they are the same feature: the public real-AI opt-in (Option
A). The day's allowance is a count in Postgres, so run 31 quietly becomes a demo
run, and only a demo tenant may ask at all.
"""

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

import relay.api.principal as principal_mod
import relay.api.ratelimit as ratelimit_mod
import relay.llm.factory as factory
from relay.agent.fake_script import _FINAL_RECORD, ENRICH_SCRIPT
from relay.agent.loop import run_agent
from relay.agent.tools import TOOL_SCHEMAS
from relay.api.main import app
from relay.config import settings
from relay.core.retry import FatalError, RetryableError
from relay.llm.gemini_client import (
    SYNTHETIC_ID_PREFIX,
    GeminiLLMClient,
    strip_signatures,
    to_api_contents,
    to_function_declarations,
)
from scripts.seed import create_api_key

MODEL = "gemini-3.5-flash-lite"


# ── a scripted "Gemini API" ──────────────────────────────────────────────
def reply(parts: list[dict[str, Any]], finish: str, **usage: int) -> dict[str, Any]:
    return {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
        "modelVersion": MODEL,
        "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200, **usage},
    }


def scripted_turn(turn: int) -> dict[str, Any]:
    """The enrichment script, as Gemini would send it: a thought part, then calls."""
    s = ENRICH_SCRIPT[turn]
    parts: list[dict[str, Any]] = [
        {"text": f"(reasoning for turn {turn})", "thought": True, "thoughtSignature": f"sig-{turn}"}
    ]
    if s.content:
        parts.append({"text": s.content})
    for tc in s.tool_calls:
        parts.append(
            {
                "functionCall": {"id": f"gcall_{tc.id}", "name": tc.name, "args": tc.args},
                "thoughtSignature": f"sig-{turn}-{tc.name}",
            }
        )
    # Gemini says STOP on a tool turn too: the functionCall part is the signal
    return reply(parts, "STOP", cachedContentTokenCount=400, thoughtsTokenCount=50)


class FakeAPI:
    """Answers like generateContent; records every request body it receives."""

    def __init__(self, answer: Callable[[dict[str, Any]], httpx2.Response] | None = None):
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.urls: list[str] = []
        self.answer = answer or self.by_turn

    @staticmethod
    def by_turn(body: dict[str, Any]) -> httpx2.Response:
        turn = sum(1 for c in body["contents"] if c["role"] == "model")
        return httpx2.Response(200, json=scripted_turn(turn))

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        self.urls.append(str(request.url))
        return self.answer(self.bodies[-1])

    def client(self) -> GeminiLLMClient:
        return GeminiLLMClient(
            client=httpx2.Client(transport=httpx2.MockTransport(self.handler)),
            api_key="test-not-a-real-key",
        )


MESSAGES = [{"role": "user", "content": "Enrich the company with domain: acmepay.example"}]


# ── conversion ───────────────────────────────────────────────────────────
def test_the_key_travels_in_a_header_never_in_the_url() -> None:
    api = FakeAPI()
    api.client().call(MODEL, "sys", MESSAGES, TOOL_SCHEMAS)
    assert api.headers[0]["x-goog-api-key"] == "test-not-a-real-key"
    assert "key=" not in api.urls[0] and "test-not-a-real-key" not in api.urls[0]
    assert api.urls[0].endswith(f"/models/{MODEL}:generateContent")


def test_the_system_prompt_rides_in_system_instruction() -> None:
    api = FakeAPI()
    api.client().call(MODEL, "be careful", MESSAGES, TOOL_SCHEMAS)
    assert api.bodies[0]["systemInstruction"] == {"parts": [{"text": "be careful"}]}
    assert api.bodies[0]["generationConfig"]["maxOutputTokens"] == (
        settings.gemini_max_output_tokens
    )


def test_roles_and_tool_results_become_gemini_contents() -> None:
    contents = to_api_contents(
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "searching",
                "tool_calls": [{"id": "c1", "name": "web_search", "args": {"query": "acme"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "web_search", "content": "[]"},
            {"role": "tool", "tool_call_id": "c2", "name": "fetch_page", "content": ""},
        ]
    )
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    # an assistant turn with no raw parts (the fake model's) is rebuilt as text + calls
    assert contents[1]["parts"] == [
        {"text": "searching"},
        {"functionCall": {"name": "web_search", "args": {"query": "acme"}}},
    ]
    # consecutive tool messages collapse into ONE user turn of functionResponses
    assert len(contents[2]["parts"]) == 2
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "web_search"
    assert contents[2]["parts"][0]["functionResponse"]["response"] == {"content": "[]"}
    assert contents[2]["parts"][0]["functionResponse"]["id"] == "c1"
    # an empty result still says something: Gemini rejects an empty response field
    assert contents[2]["parts"][1]["functionResponse"]["response"] == {"content": "(empty result)"}


def test_a_relay_minted_call_id_is_not_echoed_back_to_gemini() -> None:
    """Gemini only gets an id back if Gemini gave us one; ours would be unknown to it."""
    contents = to_api_contents(
        [
            {"role": "user", "content": "go"},
            {
                "role": "tool",
                "tool_call_id": f"{SYNTHETIC_ID_PREFIX}0-1",
                "name": "web_search",
                "content": "[]",
            },
        ]
    )
    assert "id" not in contents[-1]["parts"][0]["functionResponse"]


def test_an_unknown_role_is_a_bug_not_a_request() -> None:
    with pytest.raises(ValueError, match="unknown message role"):
        to_api_contents([{"role": "system", "content": "hi"}])


def test_every_real_tool_schema_converts() -> None:
    """The whole live toolbox, including the Pydantic shapes Gemini is fussy about."""
    declarations = to_function_declarations(TOOL_SCHEMAS)
    assert [d["name"] for d in declarations] == [t["name"] for t in TOOL_SCHEMAS]
    for d in declarations:
        assert "parameters" not in d  # mixing the two parameter fields is rejected
        schema = d["parametersJsonSchema"]
        assert schema["type"] == "object"
        assert "title" not in json.dumps(schema)  # Pydantic's titles are stripped everywhere

    by_name = {d["name"]: d["parametersJsonSchema"] for d in declarations}
    record = by_name["save_company"]["properties"]
    # the three shapes a cleaned-down OpenAPI schema would have lost
    assert record["hq_country"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert record["confidence"]["additionalProperties"] == {"type": "number"}
    assert record["sources"]["items"]["format"] == "uri"
    assert record["founded_year"]["anyOf"][0]["minimum"] == 1600
    assert by_name["save_company"]["required"] == ["name", "description", "sources", "confidence"]


# ── one reply, read back ─────────────────────────────────────────────────
def test_a_tool_call_turn_comes_back_as_tool_use_with_its_parts_kept() -> None:
    api = FakeAPI()
    response = api.client().call(MODEL, "sys", MESSAGES, TOOL_SCHEMAS)
    assert response.stop_reason == "tool_use"
    assert [tc.name for tc in response.tool_calls] == ["web_search"]
    assert response.tool_calls[0].id == "gcall_call_1"
    assert response.tool_calls[0].args == {"query": "Acme Payments"}
    # the thought part is replayed but never read as the answer
    assert response.content == "I'll start by searching for the company."
    assert response.raw_content is not None
    assert response.raw_content[0]["thoughtSignature"] == "sig-0"
    assert response.served_model == MODEL
    # promptTokenCount includes the cached prefix; Relay splits them for pricing
    assert (response.input_tokens, response.cache_read_tokens) == (600, 400)
    assert response.output_tokens == 250  # candidates + thoughts: Google bills both as output
    assert response.cache_write_tokens == 0


def test_a_call_without_an_id_gets_a_deterministic_one() -> None:
    parts = [{"functionCall": {"name": "web_search", "args": {"query": "x"}}}]
    api = FakeAPI(lambda _b: httpx2.Response(200, json=reply(parts, "STOP")))
    first = api.client().call(MODEL, "sys", MESSAGES, TOOL_SCHEMAS)
    again = api.client().call(MODEL, "sys", MESSAGES, TOOL_SCHEMAS)
    # same position in the same conversation -> same id, so the tool's
    # idempotency key ('<run_id>:<id>') survives a crash and a replay
    assert first.tool_calls[0].id == again.tool_calls[0].id == f"{SYNTHETIC_ID_PREFIX}0-0"


@pytest.mark.parametrize(
    ("finish", "expected"),
    [
        ("STOP", "end_turn"),
        ("MAX_TOKENS", "max_tokens"),
        ("SAFETY", "refusal"),
        ("RECITATION", "refusal"),
        ("BLOCKLIST", "refusal"),
        ("PROHIBITED_CONTENT", "refusal"),
        ("MALFORMED_FUNCTION_CALL", "other"),
        ("", "other"),
    ],
)
def test_stop_reasons_map_to_relays_five(finish: str, expected: str) -> None:
    api = FakeAPI(lambda _b: httpx2.Response(200, json=reply([{"text": "x"}], finish)))
    assert api.client().call(MODEL, "sys", MESSAGES, TOOL_SCHEMAS).stop_reason == expected


def test_a_function_call_beats_the_finish_reason() -> None:
    """A reply with something to run is a tool turn whatever the finish reason says."""
    parts = [{"functionCall": {"id": "g1", "name": "web_search", "args": {}}}]
    api = FakeAPI(lambda _b: httpx2.Response(200, json=reply(parts, "MAX_TOKENS")))
    assert api.client().call(MODEL, "sys", MESSAGES, TOOL_SCHEMAS).stop_reason == "tool_use"


def test_a_blocked_prompt_is_a_refusal_not_a_crash() -> None:
    body = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 10}}
    api = FakeAPI(lambda _b: httpx2.Response(200, json=body))
    response = api.client().call(MODEL, "sys", MESSAGES, TOOL_SCHEMAS)
    assert response.stop_reason == "refusal" and response.content == ""


# ── errors ───────────────────────────────────────────────────────────────
def quota_error(quota_id: str) -> dict[str, Any]:
    return {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "You exceeded your current quota.",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": quota_id}],
                }
            ],
        }
    }


def test_a_per_minute_429_is_retryable() -> None:
    body = quota_error("GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
    api = FakeAPI(lambda _b: httpx2.Response(429, json=body))
    with pytest.raises(RetryableError, match="model unavailable"):
        api.client().call(MODEL, "sys", MESSAGES, TOOL_SCHEMAS)


def test_a_daily_quota_429_is_fatal_so_it_cannot_burn_every_retry() -> None:
    body = quota_error("GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    api = FakeAPI(lambda _b: httpx2.Response(429, json=body))
    with pytest.raises(FatalError, match="daily quota exhausted"):
        api.client().call(MODEL, "sys", MESSAGES, TOOL_SCHEMAS)


@pytest.mark.parametrize("status", [408, 500, 503])
def test_transient_statuses_are_retryable(status: int) -> None:
    api = FakeAPI(lambda _b: httpx2.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(RetryableError):
        api.client().call(MODEL, "sys", MESSAGES, TOOL_SCHEMAS)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_statuses_are_fatal(status: int) -> None:
    api = FakeAPI(lambda _b: httpx2.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(FatalError, match="model request rejected"):
        api.client().call(MODEL, "sys", MESSAGES, TOOL_SCHEMAS)


def test_an_error_never_quotes_a_header() -> None:
    body = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "bad key"}}
    api = FakeAPI(lambda _b: httpx2.Response(403, json=body))
    with pytest.raises(FatalError) as caught:
        api.client().call(MODEL, "sys", MESSAGES, TOOL_SCHEMAS)
    assert "x-goog-api-key" not in str(caught.value) and "test-not-a-real-key" not in str(
        caught.value
    )


def test_a_timeout_is_retryable() -> None:
    def times_out(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.TimeoutException("too slow")

    client = GeminiLLMClient(
        client=httpx2.Client(transport=httpx2.MockTransport(times_out)), api_key="k"
    )
    with pytest.raises(RetryableError, match="timeout"):
        client.call(MODEL, "sys", MESSAGES, TOOL_SCHEMAS)


def test_no_key_fails_at_construction_before_any_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY is not set"):
        GeminiLLMClient()


def test_a_refused_signature_is_stripped_and_retried_once() -> None:
    attempts: list[dict[str, Any]] = []

    def refuse_then_accept(body: dict[str, Any]) -> httpx2.Response:
        attempts.append(body)
        if len(attempts) == 1:
            return httpx2.Response(
                400, json={"error": {"message": "Invalid thought signature for this request"}}
            )
        return httpx2.Response(200, json=reply([{"text": "ok"}], "STOP"))

    api = FakeAPI(refuse_then_accept)
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "hm",
            "tool_calls": [],
            "raw": [
                {"text": "think", "thought": True, "thoughtSignature": "sig-0"},
                {"text": "hm", "thoughtSignature": "sig-0b"},
            ],
        },
        {"role": "user", "content": "carry on"},
    ]
    assert api.client().call(MODEL, "sys", history, TOOL_SCHEMAS).content == "ok"
    assert len(attempts) == 2
    assert "thoughtSignature" in json.dumps(attempts[0]["contents"])
    assert "thoughtSignature" not in json.dumps(attempts[1]["contents"])
    # stripping drops the private thought part and keeps the turn's own text
    assert attempts[1]["contents"][1]["parts"] == [{"text": "hm"}]


def test_stripping_leaves_other_roles_alone() -> None:
    contents = [{"role": "user", "parts": [{"text": "hi", "thoughtSignature": "x"}]}]
    assert strip_signatures(contents) == contents


# ── the loop, end to end, with a crash in the middle ─────────────────────
class Crash(Exception):
    """Stands in for kill -9 at the top of a turn."""


@pytest.fixture
def real_model(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAPI]:
    api = FakeAPI()
    monkeypatch.setattr(factory.settings, "llm_provider", "gemini")
    monkeypatch.setattr(factory.settings, "gemini_model", MODEL)
    monkeypatch.setitem(factory._clients, "gemini", api.client())
    yield api


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
    assert len(real_model.bodies) == 2  # turns 0 and 1 were metered

    result = run_agent(run_id, domain, "worker-B", lambda: True, budget_usd=1.0)
    assert result is not None and result["name"] == _FINAL_RECORD["name"]
    bodies = real_model.bodies
    assert len(bodies) == 4  # turns 2 and 3 only: nothing was asked for twice

    for prev, nxt in zip(bodies, bodies[1:], strict=False):
        # append-only: the earlier request's contents are an exact prefix of the next one's
        assert nxt["contents"][: len(prev["contents"])] == prev["contents"]
        assert nxt["systemInstruction"] == prev["systemInstruction"]
        assert nxt["tools"] == prev["tools"]

    # the thought signatures written by worker A are what worker B sent back, unchanged
    model_turns = [c for c in bodies[3]["contents"] if c["role"] == "model"]
    assert [t["parts"][0]["thoughtSignature"] for t in model_turns] == ["sig-0", "sig-1", "sig-2"]
    assert [
        p["thoughtSignature"] for t in model_turns for p in t["parts"] if "functionCall" in p
    ] == [
        "sig-0-web_search",
        "sig-1-fetch_page",
        "sig-1-fetch_page",
        "sig-2-save_company",
    ]


def test_a_billed_refusal_is_recorded_then_fails_the_run(
    parked_run: tuple[uuid.UUID, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.check_run import load_steps

    api = FakeAPI(lambda _b: httpx2.Response(200, json=reply([], "SAFETY")))
    monkeypatch.setattr(factory.settings, "llm_provider", "gemini")
    monkeypatch.setattr(factory.settings, "gemini_model", MODEL)
    monkeypatch.setitem(factory._clients, "gemini", api.client())
    run_id, domain = parked_run
    assert run_agent(run_id, domain, "w", lambda: True, budget_usd=1.0) is None
    steps = load_steps(str(run_id))
    assert [s["kind"] for s in steps] == ["run_started", "model_call", "error"]
    assert steps[1]["payload"]["cost_usd"] > 0  # metered at the paid rate, in the ledger
    assert steps[2]["payload"]["reason"] == "model_refused"


def test_live_mode_without_a_key_fails_the_run_not_the_worker(
    parked_run: tuple[uuid.UUID, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.check_run import load_steps

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(factory, "_clients", {})  # no cached client to fall back on
    run_id, domain = parked_run
    assert run_agent(run_id, domain, "w", lambda: True, budget_usd=1.0, mode="live") is None
    last = load_steps(str(run_id))[-1]
    assert last["kind"] == "error" and last["payload"]["reason"] == "model_misconfigured"


def test_each_provider_escalates_only_inside_its_own_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P5 on Gemini: a Gemini run gets its own pair; a Claude id is never handed to it."""
    assert factory.escalation_model("gemini") == "gemini-3.8-flash"
    assert factory.fallback_model("gemini") == "gemini-3.8-flash"
    assert factory.escalation_model("anthropic") == "claude-sonnet-5-5"
    assert factory.escalation_model("fake") == "" and factory.fallback_model("fake") == ""
    assert factory.resolve_mode("public_real") == ("gemini", "fake")

    monkeypatch.setattr(factory.settings, "gemini_escalation_model", "claude-sonnet-5-5")
    monkeypatch.setattr(factory.settings, "fallback_model", "gemini-3.8-flash")
    assert factory.escalation_model("gemini") == ""  # cross-family: off, not attempted
    assert factory.fallback_model("anthropic") == ""
    warnings = factory.preflight()
    assert any("'claude-sonnet-5-5' is not a gemini model" in w for w in warnings)


LITE, FLASH = MODEL, "gemini-3.8-flash"
BROKEN = json.dumps({**_FINAL_RECORD, "founded_year": 1599})  # valid JSON, invalid value


def ladder_api(strong_answer: str) -> FakeAPI:
    """Flash-Lite always answers BROKEN, with a signed thought; 3.8 Flash `strong_answer`."""
    api = FakeAPI()

    def answer(body: dict[str, Any]) -> httpx2.Response:
        model = api.urls[-1].split("/models/")[1].split(":")[0]
        turn = sum(1 for c in body["contents"] if c["role"] == "model")
        text = BROKEN if model == LITE else strong_answer
        parts: list[dict[str, Any]] = [
            {"text": "(checking)", "thought": True, "thoughtSignature": f"{model}-sig-{turn}"},
            {"text": text},
        ]
        return httpx2.Response(200, json=reply(parts, "STOP"))

    api.answer = answer
    return api


def test_rung_3_on_gemini_asks_3_8_flash_with_the_history_verbatim(
    parked_run: tuple[uuid.UUID, str], real_model: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real adapter: the third request goes to 3.8 Flash, append-only."""
    from scripts.check_run import find_problems, load_steps

    api = ladder_api(json.dumps(_FINAL_RECORD))
    monkeypatch.setitem(factory._clients, "gemini", api.client())
    run_id, domain = parked_run

    result = run_agent(run_id, domain, "w", lambda: True, budget_usd=1.0, mode="public_real")

    assert result is not None and result["founded_year"] == _FINAL_RECORD["founded_year"]
    assert [u.split("/models/")[1] for u in api.urls] == [
        f"{LITE}:generateContent",
        f"{LITE}:generateContent",  # rung 2: the re-ask, same model
        f"{FLASH}:generateContent",  # rung 3: the stronger model
    ]
    first, second, third = api.bodies
    assert third["contents"][: len(second["contents"])] == second["contents"]
    assert third["systemInstruction"] == first["systemInstruction"]
    # Flash-Lite's signed turns reach 3.8 Flash byte-for-byte (P7's replay rule)
    signed = [c["parts"][0]["thoughtSignature"] for c in third["contents"] if c["role"] == "model"]
    assert signed == [f"{LITE}-sig-0", f"{LITE}-sig-1"]
    steps = load_steps(str(run_id))
    final = next(s["payload"] for s in steps if s["kind"] == "final")
    assert final["repaired_by"] == ["model_reask", f"escalated:{FLASH}"]
    assert find_problems(steps) == []


def test_a_claude_id_configured_for_gemini_is_never_called(
    parked_run: tuple[uuid.UUID, str], real_model: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.check_run import load_steps

    monkeypatch.setattr(factory.settings, "gemini_escalation_model", "claude-sonnet-5-5")
    api = ladder_api(json.dumps(_FINAL_RECORD))
    monkeypatch.setitem(factory._clients, "gemini", api.client())
    run_id, domain = parked_run

    assert run_agent(run_id, domain, "w", lambda: True, budget_usd=1.0, mode="public_real") is None
    assert all(f"/models/{LITE}:" in u for u in api.urls) and len(api.urls) == 2
    assert load_steps(str(run_id))[-1]["payload"]["reason"] == "invalid_final_answer"


# ── Option A: the public real-AI opt-in ──────────────────────────────────
@pytest.fixture
def public_tenant(db: None, request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    """A fresh tenant with its own key, so the day's count starts at zero."""
    from relay.db import pool

    mode = getattr(request, "param", "demo")
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (name, run_mode) VALUES (%s, %s) RETURNING id",
            (f"pytest-realai-{uuid.uuid4().hex[:6]}", mode),
        )
        row = cur.fetchone()
        assert row is not None
        key = create_api_key(cur, row[0])
    principal_mod._cache.clear()
    ratelimit_mod._windows.clear()
    yield {"id": row[0], "headers": {"Authorization": f"Bearer {key}"}}
    with pool.connection() as conn:  # park: no live worker may pick these up
        conn.execute("UPDATE runs SET status = 'dead' WHERE tenant_id = %s", (row[0],))


def start(tenant: dict[str, Any], real_ai: bool = True, domain: str = "p.example") -> Any:
    return TestClient(app).post(
        "/v1/runs", json={"domain": domain, "real_ai": real_ai}, headers=tenant["headers"]
    )


def park(tenant: dict[str, Any]) -> None:
    """Finish this tenant's runs and backdate them, as if the pacing gap had passed.

    The gap is measured from created_at, so a test that wants the NEXT run has to
    move the clock rather than wait 45 real seconds. Only just past the gap: the
    daily cap resets at midnight Pacific, so an hour back would drop the run out
    of "today" during the first hour of every Pacific day.
    """
    from relay.api import repo
    from relay.db import pool

    with pool.connection() as conn:
        conn.execute(
            "UPDATE runs SET status = 'dead', "
            "created_at = created_at - make_interval(secs => %s) WHERE tenant_id = %s",
            (repo.PUBLIC_REAL_MIN_GAP_SECONDS + 1, tenant["id"]),
        )


def test_an_opted_in_run_is_public_real_and_spends_one_of_the_day(
    public_tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from psycopg.rows import dict_row

    from relay.db import pool

    monkeypatch.setattr(settings, "public_real_runs_per_day", 3)
    response = start(public_tenant)
    assert response.status_code == 202
    assert response.json()["mode"] == "public_real"
    assert response.json()["real_ai_runs_left"] == 2  # after this one
    assert response.json()["note"] is None

    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "SELECT input, budget_usd FROM runs WHERE id = %s", (response.json()["run_id"],)
        ).fetchone()
    assert row is not None
    assert row["input"]["mode"] == "public_real"  # the run keeps its mode for life
    assert float(row["budget_usd"]) == settings.live_run_budget_usd
    assert factory.resolve_mode("public_real") == ("gemini", "fake")  # pretend internet


def test_run_31_quietly_gets_the_demo_model(
    public_tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "public_real_runs_per_day", 2)  # a 2-run day, same arithmetic
    for expected_left in (1, 0):
        response = start(public_tenant)
        assert response.status_code == 202
        assert response.json()["mode"] == "public_real"
        assert response.json()["real_ai_runs_left"] == expected_left
        park(public_tenant)

    spent = start(public_tenant)  # one past the cap
    assert spent.status_code == 202  # still a run, just not a real-AI one
    assert spent.json()["mode"] == "demo"
    assert spent.json()["real_ai_runs_left"] == 0
    assert "used up" in spent.json()["note"]


def test_the_cap_is_counted_per_day_not_per_process(
    public_tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count is a query, so a restarted API process cannot hand out more."""
    from relay.api import repo

    monkeypatch.setattr(settings, "public_real_runs_per_day", 5)
    assert repo.real_runs_left(public_tenant["id"]) == 5
    start(public_tenant)
    park(public_tenant)
    assert repo.real_runs_left(public_tenant["id"]) == 4  # read straight from Postgres

    monkeypatch.setattr(settings, "public_real_runs_per_day", 0)
    assert repo.real_runs_left(public_tenant["id"]) == 0  # the switch is off: nothing offered


def test_real_ai_runs_are_spaced_for_the_per_minute_quota(
    public_tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The free tier is limited per minute too, so runs don't start back to back."""
    from relay.api import repo

    monkeypatch.setattr(settings, "public_real_runs_per_day", 30)
    assert start(public_tenant).status_code == 202
    busy = start(public_tenant, domain="q.example")
    assert busy.status_code == 429
    assert busy.headers["Retry-After"] == "15"
    assert "per-minute quota" in busy.json()["detail"]
    # nothing was created and no allowance was spent
    assert repo.real_runs_left(public_tenant["id"]) == 29

    park(public_tenant)  # the gap has passed
    assert start(public_tenant, domain="r.example").status_code == 202
    assert repo.real_runs_left(public_tenant["id"]) == 28


def test_a_hung_real_ai_run_stops_holding_the_gate(
    public_tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap is measured from creation, so one stuck run can't close the day."""
    from relay.api import repo
    from relay.db import pool

    monkeypatch.setattr(settings, "public_real_runs_per_day", 30)
    stuck = start(public_tenant).json()["run_id"]
    with pool.connection() as conn:  # still 'running', but started long ago
        conn.execute(
            "UPDATE runs SET status = 'running', created_at = created_at - interval "
            f"'{repo.PUBLIC_REAL_MIN_GAP_SECONDS + 5} seconds' WHERE id = %s",
            (stuck,),
        )
    assert start(public_tenant, domain="q.example").json()["mode"] == "public_real"


def test_canned_tool_runs_are_told_the_internet_is_canned() -> None:
    """Measured: without this note a real model searches six times for nothing."""
    from relay.agent.prompts import SYSTEM_PROMPT, system_prompt

    assert system_prompt("real") == SYSTEM_PROMPT  # P1's exact bytes, untouched
    canned = system_prompt("fake")
    assert canned.startswith(SYSTEM_PROMPT)  # the note is appended, never woven in
    assert "no live internet" in canned and "at most ONCE" in canned
    # the prompt is part of every signature's prefix, so it must be stable per run
    assert system_prompt("fake") == canned


def test_the_canned_search_says_it_is_canned_instead_of_answering_nothing() -> None:
    """An empty list makes a real model rephrase and retry; the truth costs one turn."""
    from relay.agent.tools import dispatch_tool

    found = dispatch_tool("web_search", {"query": "Acme Payments"})
    assert found.ok and "acmepay.example" in found.content  # the canned query still works

    missing = dispatch_tool("web_search", {"query": "stripe.com revenue"})
    assert missing.ok is False
    assert "canned" in missing.content and "Rephrasing will not help" in missing.content
    assert missing.attempts == 1  # a healthy tool with no answer is not retried
    assert missing.breaker_open is False  # and its breaker is untouched


def test_the_default_run_is_still_the_instant_demo(public_tenant: dict[str, Any]) -> None:
    response = start(public_tenant, real_ai=False)
    assert response.status_code == 202 and response.json()["mode"] == "demo"


@pytest.mark.parametrize("public_tenant", ["live"], indirect=True)
def test_a_live_tenant_cannot_use_the_public_opt_in(public_tenant: dict[str, Any]) -> None:
    """The opt-in is the demo page's; a live tenant already runs the real model."""
    response = start(public_tenant)
    assert response.status_code == 409
    assert "live-mode" in response.json()["detail"]


def test_the_opt_in_does_not_change_pre_p7_idempotency_hashes() -> None:
    """A key minted before P7 must still hash to what it hashed then."""
    from relay.api.repo import request_hash

    plain = request_hash({"domain": "x.example"})
    assert plain == request_hash({"domain": "x.example"} | {})  # real_ai off: nothing added
    assert plain != request_hash({"domain": "x.example", "real_ai": True})
