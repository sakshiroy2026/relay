"""Check one run's ledger for the Day 6 guarantees (and, since Day 16, its shape).

Usage: python -m scripts.check_run <run_id>

Checks: step indexes are 0..n-1 with no gaps, and no model reply was paid for
twice (no two model_call rows with the same content + tool calls). Day 16 adds
the shape checks a stale worker could break if fencing failed: the ledger starts
with run_started, every tool_call / tool_result answers the latest model_call,
no tool call is confirmed twice, and nothing follows a verdict. Prints a summary
and exits 1 if anything is wrong. The chaos harnesses import check_run().
"""

import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from psycopg.rows import dict_row

from relay.db import pool

TERMINAL = ("final", "error", "budget_exceeded")


def load_steps(run_id: str) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT step_index, kind, payload, written_by FROM steps "
                "WHERE run_id = %s ORDER BY step_index",
                (run_id,),
            )
            return cur.fetchall()


def find_problems(steps: Sequence[Mapping[str, Any]]) -> list[str]:
    """Pure: the ledger rows (sorted by step_index) in, a list of problems out."""
    problems: list[str] = []

    indexes = [s["step_index"] for s in steps]
    if indexes != list(range(len(indexes))):
        problems.append(f"index gaps or duplicates: {indexes}")
    if steps and steps[0]["kind"] != "run_started":
        problems.append(f"step 0 is {steps[0]['kind']!r}, not run_started")

    seen: dict[str, int] = {}
    asked: set[str] = set()  # tool call ids the latest model_call asked for
    confirmed: set[str] = set()  # ... of those, the ones with a tool_result
    verdict_at: int | None = None
    for s in steps:
        i, kind, p = s["step_index"], s["kind"], s["payload"]
        if verdict_at is not None:
            problems.append(f"{kind} at step {i} comes after the verdict at step {verdict_at}")
            continue

        if kind == "model_call":
            key = json.dumps([p["content"], p["tool_calls"]], sort_keys=True)
            if key in seen:
                problems.append(f"model_call at step {i} repeats step {seen[key]}")
            else:
                seen[key] = i
            asked = {tc["id"] for tc in p["tool_calls"]}
            confirmed = set()
        elif kind in ("tool_call", "tool_result"):
            call_id = p["tool_call_id"]
            if call_id not in asked:
                problems.append(f"{kind} at step {i} answers {call_id!r}, never asked for")
            elif kind == "tool_result":
                if call_id in confirmed:
                    problems.append(f"tool_result at step {i}: {call_id!r} confirmed twice")
                confirmed.add(call_id)
        elif kind in TERMINAL:
            verdict_at = i

    return problems


def check_run(run_id: str) -> list[str]:
    """Return a list of problems; empty means the ledger is clean."""
    return find_problems(load_steps(run_id))


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m scripts.check_run <run_id>")
        sys.exit(2)
    run_id = sys.argv[1]

    try:
        steps = load_steps(run_id)
        problems = find_problems(steps)
    finally:
        pool.close()

    kinds = [s["kind"] for s in steps]
    writers = list(dict.fromkeys(s["written_by"] for s in steps))  # in order of first appearance
    print(f"{len(steps)} steps, {kinds.count('model_call')} model_call, writers: {writers}")

    if problems:
        for p in problems:
            print(f"FAIL  {p}")
        sys.exit(1)
    print("OK    no gaps, no duplicate model_call, ledger shape valid")


if __name__ == "__main__":
    main()
