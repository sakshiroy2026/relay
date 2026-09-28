"""Relay worker — claim a run, run the agent loop, record how it ended."""

import os
import socket
import time
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from relay.agent.loop import run_agent
from relay.config import settings
from relay.core.ledger import LeaseLost
from relay.db import pool

# ── 1. settings ──────────────────────────────────────────────────────────
LEASE_SECONDS = settings.lease_seconds
POLL_INTERVAL_SECONDS = 0.5
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


# ── 4. finish (both guarded: only the current owner of a running run may finish it)
def mark_succeeded(run_id: UUID, result: dict[str, Any]) -> bool:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE runs
                SET status = 'succeeded', result = %s, finished_at = now()
                WHERE id = %s AND lease_owner = %s AND status = 'running'
                """,
                (Jsonb(result), run_id, WORKER_ID),
            )
            updated = cur.rowcount  # how many rows the UPDATE changed: 0 or 1
    return updated == 1


def mark_failed(run_id: UUID) -> bool:
    # the reason is already in the ledger's last `error` step
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE runs
                SET status = 'failed', finished_at = now()
                WHERE id = %s AND lease_owner = %s AND status = 'running'
                """,
                (run_id, WORKER_ID),
            )
            updated = cur.rowcount
    return updated == 1


# ── 5. one run ───────────────────────────────────────────────────────────
def run_once() -> bool:
    run = claim_run()  # B
    if run is None:
        return False

    run_id = run["id"]
    domain = run["input"]["domain"]  # E
    print(f"[{WORKER_ID}] claimed {run_id} (attempt {run['attempt_count']})", flush=True)

    def beat() -> bool:  # F — handed to the loop, called at the top of every turn
        return extend_lease(run_id)

    try:
        result = run_agent(run_id, domain, WORKER_ID, beat)  # A
    except LeaseLost as exc:  # D
        print(f"[{WORKER_ID}] lease_lost {str(run_id)[:8]}: {exc}", flush=True)
        return True

    if result is None:  # C
        ok = mark_failed(run_id)
        outcome = "failed"
    else:  # G
        ok = mark_succeeded(run_id, result)
        outcome = "finished"

    if ok:
        print(f"[{WORKER_ID}] {outcome} {run_id}", flush=True)
    else:
        print(f"[{WORKER_ID}] LOST {run_id}: lease no longer mine", flush=True)
    return True  # H


# ── 6. the poll loop ─────────────────────────────────────────────────────
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
