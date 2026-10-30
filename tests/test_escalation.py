"""P5: the stronger model as a last resort — repair rung 3 and the model fallback.

A scripted client stands in for the provider and records which model each call
asked for. What must hold, for BOTH real providers (Claude: Haiku -> Sonnet;
Gemini since P5-on-Gemini: 3.5 Flash-Lite -> 3.8 Flash): the stronger model is
used only after the cheap one's own chances are spent, only when the run's
remaining budget covers a pessimistic estimate of that call, and only from the
run's own provider family; a resumed run stays escalated (the ledger
remembers); every call, on whichever model, is priced and recorded.
"""

import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest

import relay.core.retry as retry_mod
import relay.llm.factory as factory
from relay.agent.fake_script import _FINAL_RECORD
from relay.agent.loop import run_agent
from relay.core.ledger import LeaseLost
from relay.core.retry import RetryableError
from relay.llm.client import Response
from relay.llm.pricing import price
from scripts.check_run import find_problems, load_steps

GOOD = json.dumps(_FINAL_RECORD)
BAD = json.dumps({**_FINAL_RECORD, "founded_year": 1599})  # valid JSON, invalid value


class Scripted:
    """reply(model, call_number) -> Response, or raises. Records every model asked."""

    def __init__(self, reply: Callable[[str, int], Response]) -> None:
        self.reply = reply
        self.models: list[str] = []

    def call(self, model: str, system: str, messages: Any, tools: Any) -> Response:
        self.models.append(model)
        return self.reply(model, len(self.models))


def answer(model: str, text: str) -> Response:
    return Response(
        model=model, content=text, stop_reason="end_turn", input_tokens=5000, output_tokens=200
    )


@dataclass
class Ladder:
    provider: str
    cheap: str
    strong: str
    # Budgets just too small for the strong model's pessimistic estimate:
    no_rung_3: float  # after two cheap calls (~6k-token prompt)
    no_fallback: float  # before any call (~1k-token prompt)
    use: Callable[[Scripted], None]


LADDERS = {
    # Haiku's two calls ~$0.0012; a pessimistic Sonnet call ~$0.032 / ~$0.022
    "anthropic": ("claude-haiku-5-5", "claude-sonnet-5-5", 0.03, 0.01),
    # Flash-Lite's two calls ~$0.004; a pessimistic 3.8 Flash call ~$0.012 / ~$0.008
    "gemini": ("gemini-3.5-flash-lite", "gemini-3.8-flash", 0.01, 0.005),
}


@pytest.fixture(params=sorted(LADDERS))
def ladder(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Ladder]:
    provider: str = request.param
    cheap, strong, no_rung_3, no_fallback = LADDERS[provider]
    monkeypatch.setattr(factory.settings, "llm_provider", provider)
    monkeypatch.setattr(factory.settings, "gemini_model", "gemini-3.5-flash-lite")
    monkeypatch.setattr(factory.settings, "anthropic_model", "claude-haiku-5-5")
    monkeypatch.setattr(retry_mod.time, "sleep", lambda _s: None)  # no real backoff waits

    def use(client: Scripted) -> None:
        monkeypatch.setitem(factory._clients, provider, client)

    yield Ladder(provider, cheap, strong, no_rung_3, no_fallback, use)


def kinds(run_id: uuid.UUID) -> list[str]:
    return [s["kind"] for s in load_steps(str(run_id))]


# ── rung 3 ───────────────────────────────────────────────────────────────
def test_rung_3_escalates_once_after_the_reask_fails(
    parked_run: tuple[uuid.UUID, str], ladder: Ladder
) -> None:
    client = Scripted(lambda model, n: answer(model, GOOD if model == ladder.strong else BAD))
    ladder.use(client)
    run_id, domain = parked_run

    result = run_agent(run_id, domain, "w", lambda: True, budget_usd=1.0)

    assert result is not None and result["founded_year"] == _FINAL_RECORD["founded_year"]
    assert client.models == [
        ladder.cheap,
        ladder.cheap,
        ladder.strong,
    ]  # answer, re-ask, then escalation
    steps = load_steps(str(run_id))
    repairs = [s["payload"] for s in steps if s["kind"] == "repair_requested"]
    assert "escalate_to" not in repairs[0] and repairs[1]["escalate_to"] == ladder.strong
    final = next(s["payload"] for s in steps if s["kind"] == "final")
    assert final["repaired_by"] == ["model_reask", f"escalated:{ladder.strong}"]
    strong_call = [s["payload"] for s in steps if s["kind"] == "model_call"][-1]
    assert strong_call["model"] == ladder.strong  # priced at the strong model's rates
    assert strong_call["cost_usd"] == price(answer(ladder.strong, GOOD))
    assert find_problems(steps) == []


def test_rung_3_is_skipped_when_the_budget_cannot_cover_it(
    parked_run: tuple[uuid.UUID, str], ladder: Ladder
) -> None:
    client = Scripted(lambda model, n: answer(model, BAD))
    ladder.use(client)
    run_id, domain = parked_run

    assert run_agent(run_id, domain, "w", lambda: True, budget_usd=ladder.no_rung_3) is None
    assert client.models == [ladder.cheap] * 2  # the strong model was never called
    last = load_steps(str(run_id))[-1]
    assert last["payload"]["reason"] == "invalid_final_answer"


def test_a_resumed_run_stays_escalated(parked_run: tuple[uuid.UUID, str], ladder: Ladder) -> None:
    class Killed(Exception):
        pass

    def dies_on_strong(model: str, n: int) -> Response:
        if model == ladder.strong:
            raise Killed  # worker A dies while waiting for the escalated call
        return answer(model, BAD)

    ladder.use(Scripted(dies_on_strong))
    run_id, domain = parked_run
    with pytest.raises(Killed):
        run_agent(run_id, domain, "worker-A", lambda: True, budget_usd=1.0)
    assert kinds(run_id)[-1] == "repair_requested"  # the escalation row is in the ledger

    resumed = Scripted(lambda model, n: answer(model, GOOD))
    ladder.use(resumed)
    assert run_agent(run_id, domain, "worker-B", lambda: True, budget_usd=1.0) is not None
    assert resumed.models == [ladder.strong]  # worker B read the escalation from the ledger


# ── model fallback ───────────────────────────────────────────────────────
def test_fallback_model_answers_when_the_primary_is_down(
    parked_run: tuple[uuid.UUID, str], ladder: Ladder
) -> None:
    def cheap_down(model: str, n: int) -> Response:
        if model == ladder.cheap:
            raise RetryableError("529 overloaded")
        return answer(model, GOOD)

    client = Scripted(cheap_down)
    ladder.use(client)
    run_id, domain = parked_run

    assert run_agent(run_id, domain, "w", lambda: True, budget_usd=1.0) is not None
    assert client.models == [ladder.cheap] * 3 + [
        ladder.strong
    ]  # 3 tries on the cheap model, then fallback
    call = next(s["payload"] for s in load_steps(str(run_id)) if s["kind"] == "model_call")
    assert call["model"] == ladder.strong and call["fallback_from"] == ladder.cheap


def test_a_lease_lost_during_the_retries_stops_the_fallback(
    parked_run: tuple[uuid.UUID, str], ladder: Ladder
) -> None:
    def cheap_down(model: str, n: int) -> Response:
        if model == ladder.cheap:
            raise RetryableError("timeout")
        return answer(model, GOOD)

    client = Scripted(cheap_down)
    ladder.use(client)
    run_id, domain = parked_run
    beats = iter([True, False])  # ours at the top of the turn, gone after the retries

    with pytest.raises(LeaseLost):
        run_agent(run_id, domain, "w", lambda: next(beats), budget_usd=1.0)
    assert ladder.strong not in client.models  # the second model was never asked


def test_no_fallback_when_unaffordable(parked_run: tuple[uuid.UUID, str], ladder: Ladder) -> None:
    def always_down(model: str, n: int) -> Response:
        raise RetryableError("529 overloaded")

    client = Scripted(always_down)
    ladder.use(client)
    run_id, domain = parked_run

    assert run_agent(run_id, domain, "w", lambda: True, budget_usd=ladder.no_fallback) is None
    assert ladder.strong not in client.models
    assert load_steps(str(run_id))[-1]["payload"]["reason"] == "model_unavailable"


def test_fallback_can_be_switched_off(
    parked_run: tuple[uuid.UUID, str],
    ladder: Ladder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(factory.settings, "fallback_model", "")
    monkeypatch.setattr(factory.settings, "gemini_fallback_model", "")
    client = Scripted(lambda model, n: (_ for _ in ()).throw(RetryableError("down")))
    ladder.use(client)
    run_id, domain = parked_run
    assert run_agent(run_id, domain, "w", lambda: True, budget_usd=1.0) is None
    assert client.models == [ladder.cheap] * 3
