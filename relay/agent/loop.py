"""The agent loop: the middleman between the model and the tools.

WHAT IT IS  One run of the enrichment agent. Asks the model, runs the tools it
            asks for, and writes every move to the ledger as it happens.
GOES IN     run_id, the domain, this worker's id (stamped as `written_by`),
            and a heartbeat function from the worker (False = lease lost).
COMES OUT   The validated company record as a dict, or None if the run
            failed (the reason is in the last `error` step).
TOUCHES     The steps table, via append_step. `messages` lives only in
            memory: that is why Day 5 is not crash-safe yet (Day 6 replay).
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
from relay.core.ledger import LeaseLost, append_step, next_step_index
from relay.llm.factory import make_llm
from relay.llm.pricing import price

MAX_TURNS = 15


def run_agent(
    run_id: UUID,
    domain: str,
    worker_id: str,
    heartbeat: Callable[[], bool],
) -> dict[str, Any] | None:
    # --- SETUP (cards I, M, D) ---
    llm = make_llm()
    index = next_step_index(run_id)  # read once; our belief from here on
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": f"Enrich the company with domain: {domain}"},
    ]

    for turn in range(MAX_TURNS):
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
        append_step(
            run_id,
            index,
            "model_call",
            {**asdict(response), "cost_usd": price(response)},
            worker_id,
        )
        index += 1

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
            try:
                record = CompanyRecord.model_validate_json(response.content)
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

        # --- H, F, A, K: for each tool the model asked for ---
        for tc in response.tool_calls:
            append_step(
                run_id,
                index,
                "tool_call",
                {"tool": tc.name, "args": tc.args, "tool_call_id": tc.id},
                worker_id,
            )
            index += 1

            outcome = dispatch_tool(tc.name, tc.args)

            append_step(
                run_id,
                index,
                "tool_result",
                {
                    "tool": tc.name,
                    "tool_call_id": tc.id,
                    "ok": outcome.ok,
                    "content": outcome.content,
                },
                worker_id,
            )
            index += 1

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": outcome.content,
                }
            )

    # --- L: MAX_TURNS passed with no final answer ---
    append_step(run_id, index, "error", {"reason": "max_turns", "max_turns": MAX_TURNS}, worker_id)
    return None
