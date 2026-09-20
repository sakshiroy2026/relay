"""Relay worker — Day 2: claim a run, pretend to work, mark it succeeded."""

import os
import socket
import time
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from relay.config import settings
from relay.core.ledger import LeaseLost, append_step, next_step_index
from relay.db import pool

# ── 1. settings ──────────────────────────────────────────────────────────
# ── 1. settings ──────────────────────────────────────────────────────────
LEASE_SECONDS = settings.lease_seconds
POLL_INTERVAL_SECONDS = 0.5
FAKE_STEPS = 6  # a run is "done" once steps 1..6 exist in the ledger
FAKE_STEP_SECONDS = 3  # stand-in for the agent loop
DB_RETRY_SECONDS = 2  # back-off after a database error

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

# ── 2. the claim query ───────────────────────────────────────────────────
CLAIM_SQL = """
UPDATE runs
SET status           = 'running',
    lease_owner      = %s,
    lease_expires_at = now() + %s * interval '1 second',
    attempt_count    = attempt_count + 1,
    started_at       = COALESCE(started_at, now())
WHERE id = (
    SELECT id FROM runs
    WHERE status IN ('pending', 'running')
      AND (lease_expires_at IS NULL OR lease_expires_at < now())
      AND attempt_count < max_attempts
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING *
"""


# ── 3. claim ─────────────────────────────────────────────────────────────
def claim_run() -> dict[str, Any] | None:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(CLAIM_SQL, (WORKER_ID, LEASE_SECONDS))
            row = cur.fetchone()
    # the `with` block has exited here → transaction committed → row lock gone, lease stays
    return row


# ── 3b. heartbeat ────────────────────────────────────────────────────────
def extend_lease(run_id: UUID) -> bool:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE runs
                SET lease_expires_at = now() + %s * interval '1 second'
                WHERE id = %s AND lease_owner = %s AND status = 'running'
                """,
                (LEASE_SECONDS, run_id, WORKER_ID),
            )
            updated = cur.rowcount  # 1 = renewed, 0 = someone else owns it now
    return updated == 1


# ── 4. finish ────────────────────────────────────────────────────────────
def mark_succeeded(run_id: UUID) -> bool:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE runs
                SET status = 'succeeded', finished_at = now()
                WHERE id = %s AND lease_owner = %s AND status = 'running'
                """,
                (run_id, WORKER_ID),
            )
            updated = cur.rowcount  # how many rows the UPDATE changed: 0 or 1
    return updated == 1


# ── 5. the loop ──────────────────────────────────────────────────────────
def do_fake_work(run_id: UUID) -> None:
    index = next_step_index(run_id)  # read ONCE: this is our belief from here on
    print(f"[{WORKER_ID}] {str(run_id)[:8]} starting at step {index}", flush=True)

    while index <= FAKE_STEPS:
        time.sleep(FAKE_STEP_SECONDS)  # stand-in for a model call or tool call
        index = append_step(run_id, index, "model_call", {"fake": True}, WORKER_ID)
        print(f"[{WORKER_ID}] {str(run_id)[:8]} wrote step {index - 1}", flush=True)

        if not extend_lease(run_id):
            raise LeaseLost(f"heartbeat failed after step {index - 1}")


def run_once() -> bool:
    run = claim_run()
    if run is None:
        return False

    run_id = run["id"]
    print(f"[{WORKER_ID}] claimed {run_id} (attempt {run['attempt_count']})", flush=True)

    try:
        do_fake_work(run_id)
    except LeaseLost as exc:
        print(f"[{WORKER_ID}] lease_lost {str(run_id)[:8]}: {exc}", flush=True)
        return True

    if mark_succeeded(run_id):
        print(f"[{WORKER_ID}] finished {run_id}", flush=True)
    else:
        print(f"[{WORKER_ID}] LOST {run_id}: lease no longer mine", flush=True)
    return True


def main() -> None:
    print(f"[{WORKER_ID}] started", flush=True)
    try:
        while True:
            try:
                did_work = run_once()
            except psycopg.OperationalError as exc:
                print(f"[{WORKER_ID}] db error, retrying in {DB_RETRY_SECONDS}s: {exc}", flush=True)
                time.sleep(DB_RETRY_SECONDS)
                continue

            if not did_work:
                time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print(f"[{WORKER_ID}] stopping", flush=True)
    finally:
        pool.close()


if __name__ == "__main__":
    main()
