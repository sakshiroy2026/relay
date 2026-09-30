"""The agent loop: the middleman between the model and the tools.

WHAT IT IS  One run of the enrichment agent. Asks the model, runs the tools it
            asks for, and writes every move to the ledger as it happens.
            Since Day 6 it starts by replaying the ledger, so a reclaimed run
            continues where the last worker stopped instead of starting over.
GOES IN     run_id, the domain, this worker's id (stamped as `written_by`),
            and a heartbeat function from the worker (False = lease lost).
COMES OUT   The validated company record as a dict, or None if the run
            failed (the reason is in the last `error` step).
TOUCHES     The steps table: one read (replay_ledger), then writes via
            append_step. `messages` is rebuilt from the ledger on every claim.
FAILS WHEN  heartbeat() is False or a step index is taken -> LeaseLost is
            raised for the worker to handle. Model mistakes never raise: bad
            tool calls come back as ok=False observations; a bad final answer
            or MAX_TURNS without one -> `error` step and None.
"""

from collections.abc import Callable
from dataclasses import asdict
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from relay.agent.prompts import SYSTEM_PROMPT
from relay.agent.schemas import CompanyRecord
from relay.agent.tools import TOOL_SCHEMAS, dispatch_tool
from relay.config import settings
from relay.core.ledger import LeaseLost, append_step
from relay.core.replay import replay_ledger
from relay.llm.client import ToolCall
from relay.llm.factory import make_llm
from relay.llm.pricing import price

MAX_TURNS = 15  # counts model_call rows already in the ledger, not just this worker's


def _run_tool(
    run_id: UUID,
    index: int,
    tc: ToolCall,
    worker_id: str,
    messages: list[dict[str, Any]],
) -> int:
    """tool_call -> run it -> tool_result -> tool message. Returns the next index."""
    index = append_step(
        run_id,
        index,
        "tool_call",
        {"tool": tc.name, "args": tc.args, "tool_call_id": tc.id},
        worker_id,
    )

    outcome = dispatch_tool(tc.name, tc.args)

    index = append_step(
        run_id,
        index,
        "tool_result",
        {"tool": tc.name, "tool_call_id": tc.id, "ok": outcome.ok, "content": outcome.content},
        worker_id,
    )
    messages.append(
        {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": outcome.content}
    )
    return index


def _judge(run_id: UUID, index: int, content: str, worker_id: str) -> dict[str, Any] | None:
    """Validate a final answer -> `final` step + record, or `error` step + None."""
    try:
        record = CompanyRecord.model_validate_json(content)
    except ValidationError as exc:
        append_step(
            run_id,
            index,
            "error",
            {"reason": "invalid_final_answer", "detail": str(exc)},
            worker_id,
        )
        return None
    result = record.model_dump(mode="json")
    append_step(run_id, index, "final", result, worker_id)
    return result


def run_agent(
    run_id: UUID,
    domain: str,
    worker_id: str,
    heartbeat: Callable[[], bool],
) -> dict[str, Any] | None:
    # --- SETUP: read the chart before doing anything ---
    # `domain` is not needed here any more: the user message is rebuilt from the
    # run_started row. Day 7's save_company uses it.
    llm = make_llm()
    state = replay_ledger(run_id)  # one read; state.next_index is our belief from here on

    # --- Resume edge cases: the run may already have a verdict ---
    if state.final is not None:  # died after `final`, before mark_succeeded
        return state.final
    if state.failed:  # an `error` row is already written
        return None

    messages = list(state.messages)
    index = state.next_index

    if state.unjudged_answer is not None:  # died after the last model_call, before judging it
        return _judge(run_id, index, state.unjudged_answer, worker_id)

    # --- Died mid-tool: run what the last reply asked for but never got back ---
    for tc in state.pending_tool_calls:
        index = _run_tool(run_id, index, tc, worker_id, messages)

    for turn in range(state.turns_used, MAX_TURNS):
        # --- C: still ours? Check before spending money. ---
        if not heartbeat():
            raise LeaseLost(f"run {run_id}: lease lost before turn {turn}")

        # --- B: ask the model ---
        response = llm.call(
            model=settings.model_planner,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=TOOL_SCHEMAS,
        )

        # --- E: write the receipt BEFORE judging the reply ---
        index = append_step(
            run_id,
            index,
            "model_call",
            {**asdict(response), "cost_usd": price(response)},
            worker_id,
        )

        # --- J: the model must remember what it asked for ---
        messages.append(
            {
                "role": "assistant",
                "content": response.content,
                "tool_calls": [asdict(tc) for tc in response.tool_calls],
            }
        )

        # --- G: final answer? Judge it and stop. ---
        if response.stop_reason == "end_turn":
            return _judge(run_id, index, response.content, worker_id)

        # --- H, F, A, K: for each tool the model asked for ---
        for tc in response.tool_calls:
            index = _run_tool(run_id, index, tc, worker_id, messages)

    # --- L: MAX_TURNS passed with no final answer ---
    append_step(run_id, index, "error", {"reason": "max_turns", "max_turns": MAX_TURNS}, worker_id)
    return None
