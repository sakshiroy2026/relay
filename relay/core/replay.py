"""Replay: rebuild a run's conversation from its ledger.

WHAT IT IS  The shift-handover read of the chart. A worker that claims a run
            reads its steps in order and rebuilds `messages` exactly as the
            loop built them in memory, so it can continue instead of restart.
GOES IN     rebuild_messages: the run's step rows (step_index, kind, payload),
            already sorted by step_index. replay_ledger: a run_id.
COMES OUT   A ReplayState: the messages, the next free step index, how many
            model turns are already used and what they cost, how many repair
            re-asks were already spent (Day 9), tool calls that were asked for
            but never confirmed, and whether the run already has a verdict
            (`final` payload, `error` / `budget_exceeded`, an end_turn reply
            not yet judged, or a billed stop with no outcome, with the
            provider's own word for it). A `requeued` row (Day 9) clears the verdict
            before it: the run was put back in the queue by an operator and
            continues from its ledger instead of restarting.
TOUCHES     replay_ledger: ONE read of the steps table. rebuild_messages:
            nothing (pure function, testable without a database).
FAILS WHEN  The ledger has no run_started row, or a row kind it doesn't know
            -> RuntimeError. Both mean a bug, not a crash, so fail loudly.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from relay.llm.client import ToolCall

STOPS_WITHOUT_OUTCOME = ("refusal", "max_tokens", "other")  # billed, nothing to act on (P1)


def assistant_message(content: str, tool_calls: list[Any], raw: Any) -> dict[str, Any]:
    """The assistant turn, built identically by the loop and by replay.

    With a real model the reply blocks (`raw`) ride along and are sent back
    verbatim: a thinking block is only valid in a byte-identical conversation.
    """
    msg: dict[str, Any] = {"role": "assistant", "content": content, "tool_calls": tool_calls}
    if raw:
        msg["raw"] = raw
    return msg


def user_message(domain: str) -> dict[str, Any]:
    """The run's opening message. Word for word what the Day 5 loop built."""
    return {"role": "user", "content": f"Enrich the company with domain: {domain}"}


@dataclass(frozen=True)
class ReplayState:
    messages: list[dict[str, Any]]
    next_index: int  # last step_index + 1
    turns_used: int  # model_call rows already in the ledger
    spent_usd: float = 0.0  # sum of cost_usd over those model_call rows (the budget's truth)
    repairs_used: int = 0  # repair_requested rows: re-asks already spent (Day 9)
    escalated_to: str | None = None  # P5: a repair_requested row moved the run to this model
    last_prompt_tokens: int = 0  # P5: the latest model_call's prompt size, for cost estimates
    pending_tool_calls: list[ToolCall] = field(default_factory=list)  # asked, never confirmed
    final: dict[str, Any] | None = None  # payload of a `final` row: the run is already done
    failed: bool = False  # an `error` / `budget_exceeded` row with no `requeued` row after it
    unjudged_answer: str | None = None  # last reply was end_turn, died before final/error
    unhandled_stop: str | None = None  # last reply was a billed refusal/max_tokens, no error yet
    unhandled_finish: str | None = None  # the provider's word for that stop, if its row has one


def rebuild_messages(rows: Sequence[Mapping[str, Any]]) -> ReplayState:
    if not rows or rows[0]["kind"] != "run_started":
        raise RuntimeError("ledger does not start with run_started")

    messages: list[dict[str, Any]] = []
    turns_used = 0
    spent = 0.0
    repairs = 0
    escalated_to: str | None = None
    last_prompt = 0
    failed = False  # an error / budget_exceeded row with no requeued row after it
    last_reply: Mapping[str, Any] | None = None  # payload of the latest model_call
    confirmed: set[str] = set()  # tool_call_ids with a tool_result since that model_call

    for row in rows:
        kind, p = row["kind"], row["payload"]

        if kind == "run_started":
            messages.append(user_message(p["domain"]))
        elif kind == "model_call":
            messages.append(assistant_message(p["content"], p["tool_calls"], p.get("raw_content")))
            turns_used += 1
            spent += float(p["cost_usd"])
            last_prompt = (
                int(p.get("input_tokens", 0))
                + int(p.get("cache_read_tokens", 0))
                + int(p.get("cache_write_tokens", 0))
            )
            last_reply = p
            confirmed = set()
        elif kind == "tool_call":
            pass  # a receipt of intent; the model never saw it
        elif kind == "tool_result":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": p["tool_call_id"],
                    "name": p["tool"],
                    "content": p["content"],
                }
            )
            confirmed.add(p["tool_call_id"])
        elif kind == "final":
            result = {k: v for k, v in p.items() if k != "repaired_by"}  # the clean record
            return ReplayState(
                messages,
                row["step_index"] + 1,
                turns_used,
                spent,
                repairs,
                escalated_to=escalated_to,
                last_prompt_tokens=last_prompt,
                final=result,
            )
        elif kind in ("error", "budget_exceeded"):
            failed = True  # final unless a requeued row follows
            last_reply = None  # the conversation ended here; nothing is pending
        elif kind == "requeued":
            failed = False  # an operator put the run back; continue from the ledger
        elif kind == "repair_requested":
            messages.append({"role": "user", "content": p["message"]})
            repairs += 1
            escalated_to = p.get("escalate_to") or escalated_to
            last_reply = None  # the broken answer was judged; the re-ask is next
        else:
            raise RuntimeError(f"replay does not know step kind {kind!r}")

    next_index = rows[-1]["step_index"] + 1

    if failed:
        return ReplayState(
            messages,
            next_index,
            turns_used,
            spent,
            repairs,
            escalated_to=escalated_to,
            last_prompt_tokens=last_prompt,
            failed=True,
        )

    # The worker died after a billed reply with no usable outcome, before failing the run.
    if last_reply is not None and last_reply["stop_reason"] in STOPS_WITHOUT_OUTCOME:
        return ReplayState(
            messages,
            next_index,
            turns_used,
            spent,
            repairs,
            escalated_to=escalated_to,
            last_prompt_tokens=last_prompt,
            unhandled_stop=last_reply["stop_reason"],
            unhandled_finish=last_reply.get("finish_reason"),
        )

    # The worker died after writing a final-looking reply but before judging it.
    if last_reply is not None and last_reply["stop_reason"] == "end_turn":
        return ReplayState(
            messages,
            next_index,
            turns_used,
            spent,
            repairs,
            escalated_to=escalated_to,
            last_prompt_tokens=last_prompt,
            unjudged_answer=last_reply["content"],
        )

    # The worker died mid-tool: calls from the last reply with no tool_result.
    pending: list[ToolCall] = []
    if last_reply is not None:
        pending = [
            ToolCall(id=tc["id"], name=tc["name"], args=tc["args"])
            for tc in last_reply["tool_calls"]
            if tc["id"] not in confirmed
        ]
    return ReplayState(
        messages,
        next_index,
        turns_used,
        spent,
        repairs,
        escalated_to=escalated_to,
        last_prompt_tokens=last_prompt,
        pending_tool_calls=pending,
    )


def replay_ledger(run_id: UUID) -> ReplayState:
    # imported here, not at the top: importing relay.db opens the Neon pool, and
    # rebuild_messages must stay unit-testable without a database
    from relay.db import pool

    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT step_index, kind, payload FROM steps WHERE run_id = %s ORDER BY step_index",
                (run_id,),
            )
            rows = cur.fetchall()
    return rebuild_messages(rows)
