"""The dead-letter queue: park runs that keep failing, and put them back on purpose.

WHAT IT IS  Two moves on the runs table ('dead' has been in run_status since
            migration 001; nothing set it until now).
            reap_exhausted: a run whose lease has expired and whose
            attempt_count reached max_attempts will never be claimed again —
            the claim query skips it. Instead of rotting invisibly, it is
            marked 'dead' with the reason on the row, and shows up in the DLQ.
            requeue_run: an operator puts a 'failed' or 'dead' run back in the
            queue. A `requeued` marker is appended to the ledger FIRST (replay
            treats the verdict before it as cleared), then the row flips to
            'pending' with max_attempts raised to attempt_count + 5. The run
            resumes from its ledger — paid model calls are not re-paid.
GOES IN     reap_exhausted: who is reaping (stamped on the marker step).
            requeue_run: the run id, who requeued (an operator name, stamped as
            written_by), optionally a new budget for budget_exceeded runs.
COMES OUT   reap_exhausted: the run ids it marked dead. requeue_run: a short
            outcome string ("requeued", or why not).
TOUCHES     runs (status flips), steps (marker rows). All single statements or
            append_step, so racing workers/operators stay consistent.
FAILS WHEN  Database errors propagate. Two operators requeueing the same run
            at once: one appends the marker, the other gets LeaseLost from the
            taken index and reports it; the status flip is idempotent.
"""

from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from relay.core.ledger import LeaseLost, append_step, next_step_index
from relay.db import pool

REQUEUE_EXTRA_ATTEMPTS = 5  # a requeued run gets this many fresh claims

# A run is reapable when no worker can ever claim it again: the lease has
# expired (the owner is gone) and the attempts are spent. The flip to 'dead'
# is the atomic claim on the reap: one worker wins, the rest match nothing.
REAP_SQL = """
UPDATE runs
SET status = 'dead',
    error = %s,
    finished_at = now()
WHERE status IN ('pending', 'running')
  AND lease_expires_at IS NOT NULL
  AND lease_expires_at < now()
  AND attempt_count >= max_attempts
RETURNING id
"""


def reap_exhausted(reaper_id: str) -> list[UUID]:
    reason = {"reason": "max_attempts_exhausted"}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(REAP_SQL, (Jsonb(reason),))
            dead = [row[0] for row in cur.fetchall()]

    for run_id in dead:  # the marker makes the DLQ visible in the ledger too
        try:
            append_step(run_id, next_step_index(run_id), "error", reason, reaper_id)
        except LeaseLost:
            pass  # someone wrote that index; the row is dead either way, runs.error has the reason
    return dead


def requeue_run(run_id: UUID, requeued_by: str, new_budget_usd: float | None = None) -> str:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT status::text AS status, attempt_count, budget_usd FROM runs WHERE id = %s",
                (run_id,),
            )
            run = cur.fetchone()
    if run is None:
        return "not found"
    if run["status"] not in ("failed", "dead"):
        return f"not requeued: status is {run['status']!r}, only failed/dead runs can be requeued"

    # Marker first, flip second. If this crashes in between, the run stays
    # failed/dead with a dangling marker — harmless, requeue again. The other
    # order would let a worker claim the run before the marker exists, and
    # replay would still see the old verdict and fail it on the spot.
    payload: dict[str, Any] = {
        "from_status": run["status"],
        "attempts_before": run["attempt_count"],
        "requeued_by": requeued_by,
    }
    if new_budget_usd is not None:
        payload["new_budget_usd"] = new_budget_usd
    try:
        append_step(run_id, next_step_index(run_id), "requeued", payload, requeued_by)
    except LeaseLost:
        return "not requeued: another requeue was writing its marker at the same time; retry"

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE runs
                SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL,
                    max_attempts = attempt_count + %s, error = NULL, finished_at = NULL,
                    budget_usd = COALESCE(%s, budget_usd)
                WHERE id = %s AND status IN ('failed', 'dead')
                """,
                (REQUEUE_EXTRA_ATTEMPTS, new_budget_usd, run_id),
            )
            flipped = cur.rowcount == 1
    return "requeued" if flipped else "requeued (status already flipped by a parallel requeue)"
