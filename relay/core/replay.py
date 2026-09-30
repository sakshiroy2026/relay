"""Replay: rebuild a run's conversation from its ledger.

WHAT IT IS  The shift-handover read of the chart. A worker that claims a run
            reads its steps in order and rebuilds `messages` exactly as the
            loop built them in memory, so it can continue instead of restart.
GOES IN     rebuild_messages: the run's step rows (step_index, kind, payload),
            already sorted by step_index. replay_ledger: a run_id.
COMES OUT   A ReplayState: the messages, the next free step index, how many
            model turns are already used, tool calls that were asked for but
            never confirmed, and whether the run already has a verdict
            (`final` payload, `error`, or an end_turn reply not yet judged).
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


def user_message(domain: str) -> dict[str, Any]:
    """The run's opening message. Word for word what the Day 5 loop built."""
    return {"role": "user", "content": f"Enrich the company with domain: {domain}"}


@dataclass(frozen=True)
class ReplayState:
    messages: list[dict[str, Any]]
    next_index: int  # last step_index + 1
    turns_used: int  # model_call rows already in the ledger
    pending_tool_calls: list[ToolCall] = field(default_factory=list)  # asked, never confirmed
    final: dict[str, Any] | None = None  # payload of a `final` row: the run is already done
    failed: bool = False  # an `error` row exists: the run already failed
    unjudged_answer: str | None = None  # last reply was end_turn, died before final/error


def rebuild_messages(rows: Sequence[Mapping[str, Any]]) -> ReplayState:
    if not rows or rows[0]["kind"] != "run_started":
        raise RuntimeError("ledger does not start with run_started")

    messages: list[dict[str, Any]] = []
    turns_used = 0
    last_reply: Mapping[str, Any] | None = None  # payload of the latest model_call
    confirmed: set[str] = set()  # tool_call_ids with a tool_result since that model_call

    for row in rows:
        kind, p = row["kind"], row["payload"]

        if kind == "run_started":
            messages.append(user_message(p["domain"]))
        elif kind == "model_call":
            messages.append(
                {"role": "assistant", "content": p["content"], "tool_calls": p["tool_calls"]}
            )
            turns_used += 1
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
            return ReplayState(messages, row["step_index"] + 1, turns_used, final=dict(p))
        elif kind == "error":
            return ReplayState(messages, row["step_index"] + 1, turns_used, failed=True)
        else:
            raise RuntimeError(f"replay does not know step kind {kind!r}")

    next_index = rows[-1]["step_index"] + 1

    # The worker died after writing a final-looking reply but before judging it.
    if last_reply is not None and last_reply["stop_reason"] == "end_turn":
        return ReplayState(messages, next_index, turns_used, unjudged_answer=last_reply["content"])

    # The worker died mid-tool: calls from the last reply with no tool_result.
    pending: list[ToolCall] = []
    if last_reply is not None:
        pending = [
            ToolCall(id=tc["id"], name=tc["name"], args=tc["args"])
            for tc in last_reply["tool_calls"]
            if tc["id"] not in confirmed
        ]
    return ReplayState(messages, next_index, turns_used, pending_tool_calls=pending)


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
