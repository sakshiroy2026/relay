"""Day 2 acceptance test: 50 runs, 3 workers, each run claimed exactly once."""

import sys
import time
import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from relay.config import settings
from relay.db import pool

# ── 1. settings ──────────────────────────────────────────────────────────
N_RUNS = 50
TIMEOUT_SECONDS = 180
CHECK_EVERY_SECONDS = 3
AGENT_VERSION = "drain-test"


# ── 2. enqueue ───────────────────────────────────────────────────────────
def enqueue(batch: str) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for i in range(N_RUNS):
                payload = {"domain": f"drain-{i}.example.com", "batch": batch}
                cur.execute(
                    """
                    INSERT INTO runs (tenant_id, input, agent_version)
                    VALUES (%s, %s, %s)
                    RETURNING id
                    """,
                    (settings.dev_tenant_id, Jsonb(payload), AGENT_VERSION),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("INSERT into runs returned no row")

                cur.execute(
                    """
                    INSERT INTO steps (run_id, step_index, kind, payload, written_by)
                    VALUES (%s, 0, 'run_started', %s, %s)
                    """,
                    (row[0], Jsonb(payload), "drain_test"),
                )
    # one commit here: all 50 runs appear to the workers at the same instant


# ── 3. progress ──────────────────────────────────────────────────────────
def count_succeeded(batch: str) -> int:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM runs
                WHERE input->>'batch' = %s AND status = 'succeeded'
                """,
                (batch,),
            )
            row = cur.fetchone()
    return 0 if row is None else row[0]


# ── 4. verdict ───────────────────────────────────────────────────────────
def report(batch: str) -> bool:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT count(*)                                  AS total,
                       count(*) FILTER (WHERE status = 'succeeded') AS succeeded,
                       count(*) FILTER (WHERE attempt_count <> 1)   AS claimed_twice,
                       max(attempt_count)                        AS max_attempts
                FROM runs
                WHERE input->>'batch' = %s
                """,
                (batch,),
            )
            summary = cur.fetchone()

            cur.execute(
                """
                SELECT lease_owner, count(*) AS runs
                FROM runs
                WHERE input->>'batch' = %s
                GROUP BY lease_owner
                ORDER BY lease_owner
                """,
                (batch,),
            )
            per_worker = cur.fetchall()

    if summary is None:
        print("FAIL: summary query returned nothing")
        return False

    print(f"total runs     : {summary['total']}")
    print(f"succeeded      : {summary['succeeded']}")
    print(f"claimed twice  : {summary['claimed_twice']}")
    print(f"max attempts   : {summary['max_attempts']}")
    print("runs per worker:")
    for w in per_worker:
        print(f"  {w['lease_owner']}: {w['runs']}")

    if len(per_worker) < 3:
        print("WARNING: fewer than 3 workers did any work — were all 3 running?")

    return (
        summary["total"] == N_RUNS
        and summary["succeeded"] == N_RUNS
        and summary["claimed_twice"] == 0
    )


# ── 5. main ──────────────────────────────────────────────────────────────
def main() -> None:
    batch = str(uuid.uuid4())
    print(f"batch {batch}: enqueueing {N_RUNS} runs...")
    enqueue(batch)

    deadline = time.monotonic() + TIMEOUT_SECONDS
    done = 0
    while time.monotonic() < deadline:
        done = count_succeeded(batch)
        print(f"  {done}/{N_RUNS} succeeded")
        if done == N_RUNS:
            break
        time.sleep(CHECK_EVERY_SECONDS)

    print()
    passed = report(batch)
    pool.close()

    print("PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
