"""Safety-slice test: a run whose budget is below the cost of one model call.

Usage: python -m scripts.budget_test   (the workers must be running)

Inserts one run directly (like drain_test) with budget_usd = BUDGET, lower than
one fake model call. Expected: the first model call is allowed (spent 0 < budget:
a call's cost is only known after it returns), the next turn's check stops the
run with a `budget_exceeded` step, and the run ends `failed` with exactly one
`model_call` in its ledger. Prints PASS/FAIL.
"""

import sys
import time

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from relay.config import settings
from relay.db import pool

BUDGET = 0.0001  # one fake planner call costs ~$0.005 (first call measured: $0.004881)
TIMEOUT_SECONDS = 60


def main() -> None:
    payload = {"domain": "budget-test.example"}
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO runs (tenant_id, input, agent_version, budget_usd)
            VALUES (%s, %s, 'budget-test', %s)
            RETURNING id
            """,
            (settings.dev_tenant_id, Jsonb(payload), BUDGET),
        ).fetchone()
        if row is None:
            raise RuntimeError("run insert returned no row")
        run_id = row[0]
        conn.execute(
            "INSERT INTO steps (run_id, step_index, kind, payload, written_by) "
            "VALUES (%s, 0, 'run_started', %s, 'budget_test')",
            (run_id, Jsonb(payload)),
        )
    print(f"run {run_id}  budget_usd={BUDGET}")

    deadline = time.monotonic() + TIMEOUT_SECONDS
    run: dict | None = None
    while time.monotonic() < deadline:
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            run = cur.execute(
                "SELECT status, spent_usd, budget_usd FROM runs WHERE id = %s", (run_id,)
            ).fetchone()
        if run and run["status"] in ("succeeded", "failed"):
            break
        time.sleep(1)

    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        steps = cur.execute(
            "SELECT step_index, kind, payload FROM steps WHERE run_id = %s ORDER BY step_index",
            (run_id,),
        ).fetchall()
    pool.close()

    kinds = [s["kind"] for s in steps]
    print(f"status={run and run['status']}  spent_usd={run and run['spent_usd']}")
    print("ledger:", kinds)
    print("last step payload:", steps[-1]["payload"])

    passed = (
        run is not None
        and run["status"] == "failed"
        and kinds[-1] == "budget_exceeded"
        and kinds.count("model_call") == 1
    )
    print("PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
