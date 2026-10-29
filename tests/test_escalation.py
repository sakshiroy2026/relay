"""P5: the stronger model as a last resort — repair rung 3 and the model fallback.

A scripted client stands in for the provider and records which model each call
asked for. What must hold: Sonnet is used only after Haiku's own chances are
spent, only on a real provider, and only when the run's remaining budget covers
a pessimistic estimate of that call; a resumed run stays escalated (the ledger
remembers); every call, on whichever model, is priced and recorded.
"""

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

import relay.core.retry as retry_mod
import relay.llm.factory as factory
from relay.agent.fake_script import _FINAL_RECORD
from relay.agent.loop import run_agent
from relay.core.retry import RetryableError
from relay.llm.client import Response
from scripts.check_run import find_problems, load_steps

HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"
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


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Scripted], None]]:
    monkeypatch.setattr(factory.settings, "llm_provider", "anthropic")
    monkeypatch.setattr(retry_mod.time, "sleep", lambda _s: None)  # no real backoff waits

    def use(client: Scripted) -> None:
        monkeypatch.setitem(factory._clients, "anthropic", client)

    yield use


def kinds(run_id: uuid.UUID) -> list[str]:
    return [s["kind"] for s in load_steps(str(run_id))]


# ── rung 3 ───────────────────────────────────────────────────────────────
def test_rung_3_escalates_once_after_the_reask_fails(
    parked_run: tuple[uuid.UUID, str], provider: Callable[[Scripted], None]
) -> None:
    client = Scripted(lambda model, n: answer(model, GOOD if model == SONNET else BAD))
    provider(client)
    run_id, domain = parked_run

    result = run_agent(run_id, domain, "w", lambda: True, budget_usd=1.0)

    assert result is not None and result["founded_year"] == _FINAL_RECORD["founded_year"]
    assert client.models == [HAIKU, HAIKU, SONNET]  # answer, re-ask, then escalation
    steps = load_steps(str(run_id))
    repairs = [s["payload"] for s in steps if s["kind"] == "repair_requested"]
    assert "escalate_to" not in repairs[0] and repairs[1]["escalate_to"] == SONNET
    final = next(s["payload"] for s in steps if s["kind"] == "final")
    assert final["repaired_by"] == ["model_reask", f"escalated:{SONNET}"]
    sonnet_call = [s["payload"] for s in steps if s["kind"] == "model_call"][-1]
    assert sonnet_call["model"] == SONNET and sonnet_call["cost_usd"] > 0.01  # Sonnet prices
    assert find_problems(steps) == []


def test_rung_3_is_skipped_when_the_budget_cannot_cover_it(
    parked_run: tuple[uuid.UUID, str], provider: Callable[[Scripted], None]
) -> None:
    client = Scripted(lambda model, n: answer(model, BAD))
    provider(client)
    run_id, domain = parked_run

    # Haiku's two calls cost ~$0.0012; a pessimistic Sonnet call ~$0.032 > what's left
    assert run_agent(run_id, domain, "w", lambda: True, budget_usd=0.03) is None
    assert client.models == [HAIKU, HAIKU]  # Sonnet was never called
    last = load_steps(str(run_id))[-1]
    assert last["payload"]["reason"] == "invalid_final_answer"


def test_a_resumed_run_stays_escalated(
    parked_run: tuple[uuid.UUID, str], provider: Callable[[Scripted], None]
) -> None:
    class Killed(Exception):
        pass

    def dies_on_sonnet(model: str, n: int) -> Response:
        if model == SONNET:
            raise Killed  # worker A dies while waiting for the escalated call
        return answer(model, BAD)

    provider(Scripted(dies_on_sonnet))
    run_id, domain = parked_run
    with pytest.raises(Killed):
        run_agent(run_id, domain, "worker-A", lambda: True, budget_usd=1.0)
    assert kinds(run_id)[-1] == "repair_requested"  # the escalation row is in the ledger

    resumed = Scripted(lambda model, n: answer(model, GOOD))
    provider(resumed)
    assert run_agent(run_id, domain, "worker-B", lambda: True, budget_usd=1.0) is not None
    assert resumed.models == [SONNET]  # worker B read the escalation from the ledger


# ── model fallback ───────────────────────────────────────────────────────
def test_fallback_model_answers_when_the_primary_is_down(
    parked_run: tuple[uuid.UUID, str], provider: Callable[[Scripted], None]
) -> None:
    def haiku_down(model: str, n: int) -> Response:
        if model == HAIKU:
            raise RetryableError("529 overloaded")
        return answer(model, GOOD)

    client = Scripted(haiku_down)
    provider(client)
    run_id, domain = parked_run

    assert run_agent(run_id, domain, "w", lambda: True, budget_usd=1.0) is not None
    assert client.models == [HAIKU, HAIKU, HAIKU, SONNET]  # 3 tries on Haiku, then fallback
    call = next(s["payload"] for s in load_steps(str(run_id)) if s["kind"] == "model_call")
    assert call["model"] == SONNET and call["fallback_from"] == HAIKU


def test_no_fallback_when_unaffordable(
    parked_run: tuple[uuid.UUID, str], provider: Callable[[Scripted], None]
) -> None:
    def always_down(model: str, n: int) -> Response:
        raise RetryableError("529 overloaded")

    client = Scripted(always_down)
    provider(client)
    run_id, domain = parked_run

    assert run_agent(run_id, domain, "w", lambda: True, budget_usd=0.01) is None
    assert SONNET not in client.models
    assert load_steps(str(run_id))[-1]["payload"]["reason"] == "model_unavailable"


def test_fallback_can_be_switched_off(
    parked_run: tuple[uuid.UUID, str],
    provider: Callable[[Scripted], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(factory.settings, "fallback_model", "")
    client = Scripted(lambda model, n: (_ for _ in ()).throw(RetryableError("down")))
    provider(client)
    run_id, domain = parked_run
    assert run_agent(run_id, domain, "w", lambda: True, budget_usd=1.0) is None
    assert client.models == [HAIKU, HAIKU, HAIKU]
