"""The agent loop: the middleman between the model and the tools.

WHAT IT IS  One run of the enrichment agent. Asks the model, runs the tools it
            asks for, and writes every move to the ledger as it happens.
            Since Day 6 it starts by replaying the ledger, so a reclaimed run
            continues where the last worker stopped instead of starting over.
GOES IN     run_id, the domain, this worker's id (stamped as `written_by`),
            a heartbeat function from the worker (False = lease lost), and the
            run's budget_usd.
COMES OUT   The validated company record as a dict, or None if the run
            failed (the reason is in the last `error` / `budget_exceeded` step).
TOUCHES     The steps table: one read (replay_ledger), then writes via
            append_step. `messages` is rebuilt from the ledger on every claim.
            Tools may write companies / tool_invocations; every tool call gets
            an idempotency key '<run_id>:<tool_call_id>' (Day 7). After each
            model call, runs.spent_usd / tokens are refreshed from the ledger.
FAILS WHEN  heartbeat() is False or a step index is taken -> LeaseLost is
            raised for the worker to handle. Model mistakes never raise: bad
            tool calls come back as ok=False observations; a final answer that
            fails validation goes through the repair ladder (Day 9: free JSON
            extraction, then one re-asked model call) before it becomes an
            `error` step and None; MAX_TURNS without one -> `error` and None. Spend already
            at or over budget_usd before a model call -> `budget_exceeded`
            step and None (one call can overshoot: its cost is only known after).
            A model call still transiently failing after 3 tries -> `error`
            step (`model_unavailable`) and None; a permanent model failure
            -> `model_fatal`, never retried (Day 8). A billed reply that is
            neither a tool turn nor an answer -> `error` step whose detail
            quotes the provider's own stop word, and None.
"""

from collections.abc import Callable
from dataclasses import asdict
from typing import Any
from uuid import UUID

from relay.agent.prompts import system_prompt
from relay.agent.tools import TOOL_SCHEMAS, ToolContext, dispatch_tool
from relay.config import settings
from relay.core.costs import refresh_run_totals
from relay.core.ledger import LeaseLost, append_step
from relay.core.repair import correction_message, parse_final
from relay.core.replay import assistant_message, replay_ledger
from relay.core.retry import FatalError, RetryableError, call_with_retries
from relay.llm.client import Response, ToolCall
from relay.llm.factory import (
    escalation_model,
    fallback_model,
    make_llm,
    planner_model,
    resolve_mode,
)
from relay.llm.pricing import estimate, price
from relay.obs.tracing import tracer

MAX_TURNS = 15  # counts model_call rows already in the ledger, not just this worker's
MODEL_RETRY_ATTEMPTS = 3  # tries per model call (Day 8); only transient failures are retried


# A billed reply that is neither a tool turn nor an answer (P1: real models only).
STOP_ERRORS = {
    "refusal": "model_refused",
    "max_tokens": "model_max_tokens",
    "other": "model_stopped",
}


def _stop_error(
    run_id: UUID, index: int, stop_reason: str, finish_reason: str | None, worker_id: str
) -> None:
    """The reply was paid for and is already in the ledger; now end the run honestly."""
    detail = f"model stopped with {stop_reason!r}"
    if finish_reason:  # the provider's own word; a row older than this field has none
        detail += f" (provider said {finish_reason!r})"
    append_step(
        run_id,
        index,
        "error",
        {"reason": STOP_ERRORS[stop_reason], "detail": detail},
        worker_id,
    )


def _run_tool(
    run_id: UUID,
    domain: str,
    index: int,
    tc: ToolCall,
    worker_id: str,
    messages: list[dict[str, Any]],
    tools_provider: str = "",
) -> int:
    """tool_call -> run it -> tool_result -> tool message. Returns the next index."""
    # Day 7: the key comes from the model's call id, NOT the step index: a re-run after
    # a crash (Day 6) writes a new tool_call row at a new index but must reuse the key.
    idem_key = f"{run_id}:{tc.id}"
    ctx = ToolContext(
        run_id=run_id,
        domain=domain,
        step_index=index,
        idem_key=idem_key,
        tools_provider=tools_provider,
    )

    index = append_step(
        run_id,
        index,
        "tool_call",
        {"tool": tc.name, "args": tc.args, "tool_call_id": tc.id, "idem_key": idem_key},
        worker_id,
    )

    with tracer().start_as_current_span(
        f"relay.tool {tc.name}", attributes={"relay.tool": tc.name}
    ) as span:
        outcome = dispatch_tool(tc.name, tc.args, ctx)
        span.set_attributes({"relay.ok": outcome.ok, "relay.attempts": outcome.attempts})

    index = append_step(
        run_id,
        index,
        "tool_result",
        {
            "tool": tc.name,
            "tool_call_id": tc.id,
            "ok": outcome.ok,
            "content": outcome.content,
            # Day 8: how hard this call was. Replay ignores extra keys.
            "attempts": outcome.attempts,
            "breaker_open": outcome.breaker_open,
        },
        worker_id,
    )
    messages.append(
        {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": outcome.content}
    )
    return index


def _judge(
    run_id: UUID,
    index: int,
    content: str,
    worker_id: str,
    messages: list[dict[str, Any]],
    repairs_used: int,
    escalate_to: str | None = None,
    escalated: str | None = None,
) -> tuple[str, dict[str, Any] | None, int]:
    """Judge a final answer through the repair ladder (Day 9, rung 3 since P5).

    Returns ("final", record, next_index)   — validated, `final` written;
            ("repair", None, next_index)    — a `repair_requested` row and the
              correction message were written; the caller's next model call is
              the rung-2 re-ask;
            ("escalate", None, next_index)  — rung 3: the re-ask failed too, and
              the caller can afford `escalate_to`; the row names that model and
              the caller's next call uses it (replay remembers it);
            ("error", None, next_index)     — out of rungs; `error` written.
    """
    record, repaired_by, errors = parse_final(content)  # rungs 0 and 1
    if record is not None:
        result = record.model_dump(mode="json")
        # how it was salvaged goes to the ledger, not into the run's result
        tags = ([repaired_by] if repaired_by else []) + (["model_reask"] if repairs_used else [])
        tags += [f"escalated:{escalated}"] if escalated else []
        payload = {**result, "repaired_by": tags} if tags else result
        index = append_step(run_id, index, "final", payload, worker_id)
        return "final", result, index

    if repairs_used < settings.repair_attempts:  # rung 2: one re-ask, paid like any turn
        message = correction_message(errors)
        index = append_step(
            run_id, index, "repair_requested", {"message": message, "errors": errors}, worker_id
        )
        messages.append({"role": "user", "content": message})
        return "repair", None, index

    if escalate_to:  # rung 3 (P5): the same correction, answered by a stronger model
        message = correction_message(errors)
        index = append_step(
            run_id,
            index,
            "repair_requested",
            {"message": message, "errors": errors, "escalate_to": escalate_to},
            worker_id,
        )
        messages.append({"role": "user", "content": message})
        return "escalate", None, index

    index = append_step(
        run_id, index, "error", {"reason": "invalid_final_answer", "detail": errors}, worker_id
    )
    return "error", None, index


def run_agent(
    run_id: UUID,
    domain: str,
    worker_id: str,
    heartbeat: Callable[[], bool],
    budget_usd: float,
    mode: str | None = None,
) -> dict[str, Any] | None:
    # --- SETUP: read the chart before doing anything ---
    # The user message is rebuilt from the run_started row; `domain` goes to the
    # tools (save_company writes under the run's own domain). `mode` (P1) picks the
    # model and tools: "demo" = fake/fake, "live" = real/real, None = process default.
    state = replay_ledger(run_id)  # one read; state.next_index is our belief from here on

    # --- Resume edge cases: the run may already have a verdict ---
    if state.final is not None:  # died after `final`, before mark_succeeded
        return state.final
    if state.failed:  # an `error` / `budget_exceeded` row is already written
        return None

    messages = list(state.messages)
    index = state.next_index
    spent = state.spent_usd  # from the ledger, so a resumed run keeps counting
    repairs_used = state.repairs_used  # re-asks are counted from the ledger too

    if state.unhandled_stop is not None:  # died after a billed refusal/max_tokens reply
        _stop_error(run_id, index, state.unhandled_stop, state.unhandled_finish, worker_id)
        return None

    try:  # before any money is spent: a misconfigured model fails the run, not the worker
        llm_provider, tools = resolve_mode(mode)
        llm = make_llm(llm_provider)
        model = planner_model(llm_provider)
    except Exception as exc:
        append_step(
            run_id, index, "error", {"reason": "model_misconfigured", "detail": str(exc)}, worker_id
        )
        return None

    # Constant for the whole run: it is part of the prefix every thought
    # signature / thinking block is bound to (P7).
    system = system_prompt(tools)

    # --- P5: a stronger model as a last resort, only on a real provider, only if affordable ---
    escalated = state.escalated_to  # from the ledger: a resumed run stays escalated
    if escalated:
        model = escalated
    last_prompt = state.last_prompt_tokens
    # this provider's own pair ("" = none): a Gemini run never gets a Claude id
    escalate_model = escalation_model(llm_provider)
    fall_model = fallback_model(llm_provider)

    def can_afford(candidate: str) -> bool:
        # pessimistic (no cache) price of one call on `candidate`, checked BEFORE it is made
        return (
            bool(candidate)
            and candidate != model
            and spent + estimate(candidate, last_prompt + 1000) <= budget_usd
        )

    def escalation() -> str | None:
        return escalate_model if not escalated and can_afford(escalate_model) else None

    if state.unjudged_answer is not None:  # died after the last model_call, before judging it
        esc = escalation()
        outcome, result, index = _judge(
            run_id, index, state.unjudged_answer, worker_id, messages, repairs_used, esc, escalated
        )
        if outcome not in ("repair", "escalate"):
            return result  # the record, or None for the error case
        repairs_used += 1  # fall through: the loop's next model call is the re-ask
        if outcome == "escalate" and esc:
            model = escalated = esc

    # --- Died mid-tool: run what the last reply asked for but never got back ---
    for tc in state.pending_tool_calls:
        index = _run_tool(run_id, domain, index, tc, worker_id, messages, tools)

    for turn in range(state.turns_used, MAX_TURNS):
        # --- C: still ours? Check before spending money. ---
        if not heartbeat():
            raise LeaseLost(f"run {run_id}: lease lost before turn {turn}")

        # --- Budget: never start a model call once the run has spent its budget ---
        if spent >= budget_usd:
            append_step(
                run_id,
                index,
                "budget_exceeded",
                {"spent_usd": round(spent, 6), "budget_usd": budget_usd},
                worker_id,
            )
            return None

        # --- B: ask the model (Day 8: retried on transient failures) ---
        # Worst-case retry sleep is ~3 s, far below the lease, so no extra
        # heartbeat is needed. A model down for longer fails the run with an
        # `error` step; nothing is charged for calls that never happened.
        def ask(use: str, turn: int = turn) -> tuple[Response, int]:
            with tracer().start_as_current_span(
                "relay.model_call",
                attributes={"gen_ai.request.model": use, "relay.turn": turn},
            ) as span:
                reply, tries = call_with_retries(
                    lambda: llm.call(
                        model=use,
                        system=system,
                        messages=messages,
                        tools=TOOL_SCHEMAS,
                    ),
                    attempts=MODEL_RETRY_ATTEMPTS,
                )
                span.set_attributes(
                    {
                        "gen_ai.usage.input_tokens": reply.input_tokens,
                        "gen_ai.usage.output_tokens": reply.output_tokens,
                        "relay.attempts": tries,
                    }
                )
            return reply, tries

        fallback_from: str | None = None
        try:
            try:
                response, attempts = ask(model)
            except RetryableError:
                # P5: the primary is still down after its retries. One turn on the
                # fallback model, if the run can afford it; the next turn tries the
                # primary again (not sticky: an outage is usually short).
                if not can_afford(fall_model):
                    raise
                # Timed-out retries can outlast the lease (P5 on Gemini: 3.8 Flash
                # was measured past the 60 s timeout), so renew it, and stop if it
                # is already gone, before asking a second model.
                if not heartbeat():
                    raise LeaseLost(f"run {run_id}: lease lost before the fallback") from None
                fallback_from = model
                response, attempts = ask(fall_model)
        except RetryableError as exc:  # still failing after every attempt
            append_step(
                run_id,
                index,
                "error",
                {"reason": "model_unavailable", "detail": str(exc)},
                worker_id,
            )
            return None
        except FatalError as exc:  # would fail identically again: never retried
            append_step(
                run_id,
                index,
                "error",
                {"reason": "model_fatal", "detail": str(exc)},
                worker_id,
            )
            return None

        # --- E: write the receipt BEFORE judging the reply ---
        cost = price(response)
        index = append_step(
            run_id,
            index,
            "model_call",
            {
                **asdict(response),
                "cost_usd": cost,
                "attempts": attempts,
                **({"fallback_from": fallback_from} if fallback_from else {}),
            },
            worker_id,
        )
        spent += cost
        last_prompt = (
            response.input_tokens + response.cache_read_tokens + response.cache_write_tokens
        )
        refresh_run_totals(run_id)  # display copy on the runs row; the ledger stays the truth

        # --- J: the model must remember what it asked for ---
        messages.append(
            assistant_message(
                response.content,
                [asdict(tc) for tc in response.tool_calls],
                response.raw_content,
            )
        )

        # --- a billed reply with no usable outcome (refusal, max_tokens): end the run ---
        if response.stop_reason in STOP_ERRORS:
            _stop_error(run_id, index, response.stop_reason, response.finish_reason, worker_id)
            return None

        # --- G: final answer? Judge it through the repair ladder. ---
        if response.stop_reason == "end_turn":
            esc = escalation()
            outcome, result, index = _judge(
                run_id, index, response.content, worker_id, messages, repairs_used, esc, escalated
            )
            if outcome not in ("repair", "escalate"):
                return result
            repairs_used += 1
            if outcome == "escalate" and esc:
                model = escalated = esc  # rung 3: the next call goes to the stronger model
            continue  # the next turn is the re-ask

        # --- H, F, A, K: for each tool the model asked for ---
        for tc in response.tool_calls:
            index = _run_tool(run_id, domain, index, tc, worker_id, messages, tools)

    # --- L: MAX_TURNS passed with no final answer ---
    append_step(run_id, index, "error", {"reason": "max_turns", "max_turns": MAX_TURNS}, worker_id)
    return None
