"""Check one run's ledger for the Day 6 guarantees.

Usage: python -m scripts.check_run <run_id>

Checks: step indexes are 0..n-1 with no gaps, and no model reply was paid for
twice (no two model_call rows with the same content + tool calls). Prints a
summary and exits 1 if anything is wrong. The chaos harness imports check_run().
"""

import json
import sys
from typing import Any

from psycopg.rows import dict_row

from relay.db import pool


def load_steps(run_id: str) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT step_index, kind, payload, written_by FROM steps "
                "WHERE run_id = %s ORDER BY step_index",
                (run_id,),
            )
            return cur.fetchall()


def check_run(run_id: str) -> list[str]:
    """Return a list of problems; empty means the ledger is clean."""
    steps = load_steps(run_id)
    problems: list[str] = []

    indexes = [s["step_index"] for s in steps]
    if indexes != list(range(len(indexes))):
        problems.append(f"index gaps or duplicates: {indexes}")

    seen: dict[str, int] = {}
    for s in steps:
        if s["kind"] != "model_call":
            continue
        p = s["payload"]
        key = json.dumps([p["content"], p["tool_calls"]], sort_keys=True)
        if key in seen:
            problems.append(f"model_call at step {s['step_index']} repeats step {seen[key]}")
        else:
            seen[key] = s["step_index"]

    return problems


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m scripts.check_run <run_id>")
        sys.exit(2)
    run_id = sys.argv[1]

    try:
        steps = load_steps(run_id)
        problems = check_run(run_id)
    finally:
        pool.close()

    kinds = [s["kind"] for s in steps]
    writers = list(dict.fromkeys(s["written_by"] for s in steps))  # in order of first appearance
    print(f"{len(steps)} steps, {kinds.count('model_call')} model_call, writers: {writers}")

    if problems:
        for p in problems:
            print(f"FAIL  {p}")
        sys.exit(1)
    print("OK    no gaps, no duplicate model_call")


if __name__ == "__main__":
    main()
