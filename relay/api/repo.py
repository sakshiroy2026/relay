"""The repository layer: every read and write the API does, scoped by tenant.

WHAT IT IS  The only place API SQL lives (Day 10). Every function takes the
            authenticated tenant_id as its first argument and puts it in the
            WHERE clause / INSERT, so a route cannot forget tenant scoping —
            the function signature demands it. Workers don't use this layer:
            they are trusted code scoped by the run they claimed.
GOES IN     The Principal's tenant_id, plus the route's parameters.
COMES OUT   Plain dicts/lists for the routes to return; None for "not found
            in THIS tenant" (another tenant's rows are indistinguishable from
            missing ones, which is the point).
TOUCHES     runs, steps, companies, idempotency_keys.
FAILS WHEN  Database errors propagate. create_run raises ConcurrencyLimit
            when the tenant's open runs are at max_concurrent_runs: the count
            and the insert happen under a per-tenant advisory transaction
            lock, so two racing requests cannot both squeeze past the cap.
            With a client Idempotency-Key, a key already used for the SAME
            body returns that run (replayed, never blocked by the cap: it
            creates nothing); for a DIFFERENT body it raises
            IdempotencyMismatch. The key row commits with the run.
            A public real-AI run (P7) that would break the day's shared cap is
            not refused: it is quietly created as a `demo` run instead, and the
            caller is told which mode it actually got. One that arrives while
            another visitor's real-AI run is still in flight raises RealAIBusy
            and creates nothing — the free tier's per-minute quota only has
            room for one at a time.
"""

import hashlib
import json
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from relay.config import settings
from relay.db import pool

AGENT_VERSION = "enrich@v2"

# Google's free-tier request-per-day quota resets at midnight Pacific, so
# Relay's own daily cap uses the same boundary: when ours reopens, theirs has.
QUOTA_TIMEZONE = "America/Los_Angeles"
_SINCE_MIDNIGHT = (
    f"created_at >= date_trunc('day', now() AT TIME ZONE '{QUOTA_TIMEZONE}') "
    f"AT TIME ZONE '{QUOTA_TIMEZONE}'"
)
_COUNT_REAL_TODAY = (
    "SELECT count(*) FROM runs "
    "WHERE tenant_id = %s AND (input->>'mode') = 'public_real' AND " + _SINCE_MIDNIGHT
)

# The free tier allows roughly ten requests a minute per project and one run is
# 6-10 model calls, so two real-AI runs at once would simply 429 each other.
# One at a time is therefore the honest limit, and the page shows the wait.
PUBLIC_REAL_CONCURRENCY = 1
# A run that is still 'running' after this long is stuck or abandoned (its
# worker died and nothing reclaimed it). It stops holding the queue shut, so one
# bad run can't close the real-AI demo for the rest of the day.
PUBLIC_REAL_STALE_MINUTES = 5
_COUNT_REAL_OPEN = (
    "SELECT count(*) FROM runs "
    "WHERE tenant_id = %s AND (input->>'mode') = 'public_real' "
    "AND status IN ('pending', 'running') "
    f"AND created_at > now() - interval '{PUBLIC_REAL_STALE_MINUTES} minutes'"
)


def real_runs_left(tenant_id: UUID) -> int:
    """How many public real-AI runs this tenant has left today (never negative).

    Read outside any lock, so it is a display number: the authoritative check
    is the identical count inside create_run's advisory lock.
    """
    cap = settings.public_real_runs_per_day
    if cap <= 0:
        return 0
    with pool.connection() as conn:
        row = conn.execute(_COUNT_REAL_TODAY, (tenant_id,)).fetchone()
    used = 0 if row is None else int(row[0])
    return max(cap - used, 0)


class IdempotencyMismatch(Exception):
    """The Idempotency-Key was already used by this tenant for a different request body."""


def request_hash(body: dict[str, Any]) -> str:
    """Same body -> same hash, whatever the key order the client sent."""
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class RealAIBusy(Exception):
    """Another visitor's real-AI run is still in flight; the free tier allows one."""


class ConcurrencyLimit(Exception):
    """The tenant already has max_concurrent_runs runs pending or running."""

    def __init__(self, open_runs: int, cap: int) -> None:
        self.open_runs, self.cap = open_runs, cap
        super().__init__(f"{open_runs} open runs, cap {cap}")


def create_run(
    tenant_id: UUID,
    domain: str,
    max_concurrent: int,
    idempotency_key: str | None = None,
    run_mode: str = "demo",
    real_ai: bool = False,
) -> dict[str, Any]:
    # What the CLIENT sent; the mode is ours. `real_ai` only joins the hash when
    # it is on, so keys minted before P7 keep hashing to exactly what they did.
    body_hash = request_hash({"domain": domain} | ({"real_ai": True} if real_ai else {}))
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # One advisory lock per tenant for the length of this transaction:
            # concurrent creates for the SAME tenant queue up for a few ms, so
            # the count below can't be stale when the insert commits. Other
            # tenants use other lock keys and are not slowed down. It also
            # serialises two requests racing with the same Idempotency-Key: the
            # second one waits, then finds the first one's committed key row.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 42))", (tenant_id,)
            )
            # P7: the day's real-AI allowance, counted under the same lock as
            # the insert below, so the last run of the day can only be handed
            # out once however many visitors ask for it at the same moment.
            cur.execute(_COUNT_REAL_TODAY, (tenant_id,))
            counted = cur.fetchone()
            used_today = 0 if counted is None else int(counted[0])
            left = max(settings.public_real_runs_per_day - used_today, 0)

            if idempotency_key is not None:
                cur.execute(
                    "SELECT k.request_hash, r.id, r.status::text FROM idempotency_keys k "
                    "JOIN runs r ON r.id = k.run_id WHERE k.tenant_id = %s AND k.key = %s",
                    (tenant_id, idempotency_key),
                )
                seen = cur.fetchone()
                if seen is not None:
                    if seen[0] != body_hash:
                        raise IdempotencyMismatch(idempotency_key)
                    # checked BEFORE the cap: a replay creates nothing, so it
                    # must not be refused for capacity
                    cur.execute("SELECT input->>'mode' FROM runs WHERE id = %s", (seen[1],))
                    seen_mode = cur.fetchone()
                    return {
                        "run_id": seen[1],
                        "status": seen[2],
                        "replayed": True,
                        "mode": (seen_mode[0] if seen_mode and seen_mode[0] else "demo"),
                        "real_ai_runs_left": left,
                    }

            cur.execute(
                "SELECT count(*) FROM runs "
                "WHERE tenant_id = %s AND status IN ('pending', 'running')",
                (tenant_id,),
            )
            row = cur.fetchone()
            open_runs = 0 if row is None else int(row[0])
            if open_runs >= max_concurrent:
                raise ConcurrencyLimit(open_runs, max_concurrent)  # rolls the lock back too

            if real_ai and left > 0:
                cur.execute(_COUNT_REAL_OPEN, (tenant_id,))
                in_flight = cur.fetchone()
                if in_flight is not None and int(in_flight[0]) >= PUBLIC_REAL_CONCURRENCY:
                    # Nothing is created and no allowance is spent: the caller
                    # waits and asks again. Rolls the advisory lock back too.
                    raise RealAIBusy
                run_mode = "public_real"
                left -= 1  # this run takes one of the day's allowance
            # real_ai asked for with nothing left: the run stays `demo` (the free
            # scripted model) and the returned `mode` tells the caller so.

            payload = {"domain": domain, "mode": run_mode}  # keeps its mode for life (P1)
            # a real model call is metered whoever asked for it: real-model runs
            # get the tight real-run budget, demo runs the table default
            budget = settings.live_run_budget_usd if run_mode in ("live", "public_real") else None
            cur.execute(
                "INSERT INTO runs (tenant_id, input, agent_version, budget_usd) "
                "VALUES (%s, %s, %s, COALESCE(%s, 0.25)) RETURNING id, status",
                (tenant_id, Jsonb(payload), AGENT_VERSION, budget),
            )
            created = cur.fetchone()
            if created is None:
                raise RuntimeError("run insert returned no row")
            cur.execute(
                "INSERT INTO steps (run_id, step_index, kind, payload, written_by) "
                "VALUES (%s, 0, 'run_started', %s, 'api')",
                (created[0], Jsonb(payload)),
            )
            if idempotency_key is not None:  # same commit as the run: never one without the other
                cur.execute(
                    "INSERT INTO idempotency_keys (tenant_id, key, request_hash, run_id) "
                    "VALUES (%s, %s, %s, %s)",
                    (tenant_id, idempotency_key, body_hash, created[0]),
                )
    return {
        "run_id": created[0],
        "status": created[1],
        "replayed": False,
        "mode": run_mode,
        "real_ai_runs_left": left,
    }


def get_run(tenant_id: UUID, run_id: UUID) -> dict[str, Any] | None:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, status, input, result, error,
                       attempt_count, next_step_index, lease_owner,
                       lease_expires_at, now() AS server_now,
                       spent_usd, budget_usd, tokens_in, tokens_out,
                       agent_version, created_at, started_at, finished_at
                FROM runs
                WHERE id = %s AND tenant_id = %s
                """,
                (run_id, tenant_id),
            )
            return cur.fetchone()


def get_run_steps(tenant_id: UUID, run_id: UUID) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT s.step_index, s.kind, s.written_by, s.created_at, s.payload
                FROM steps s
                JOIN runs r ON r.id = s.run_id
                WHERE s.run_id = %s AND r.tenant_id = %s
                ORDER BY s.step_index
                """,
                (run_id, tenant_id),
            )
            return cur.fetchall()


def request_crash(tenant_id: UUID, run_id: UUID) -> str | None:
    """Demo page: ask the worker that owns this run to die at its next heartbeat.

    Returns "requested", "not_running" (exists but has no live owner to kill),
    or None (no such run in this tenant). The route has already checked the
    tenant is demo-mode; this only flips the flag the worker polls.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE runs SET crash_requested_at = now()
                WHERE id = %s AND tenant_id = %s
                  AND status = 'running' AND lease_owner IS NOT NULL
                """,
                (run_id, tenant_id),
            )
            if cur.rowcount == 1:
                return "requested"
            cur.execute(
                "SELECT 1 FROM runs WHERE id = %s AND tenant_id = %s",
                (run_id, tenant_id),
            )
            return None if cur.fetchone() is None else "not_running"


def get_company(tenant_id: UUID, domain: str) -> dict[str, Any] | None:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, domain, name, hq_country, founded_year, industry,
                       employee_range, funding_stage, description, sources, confidence,
                       created_by_run, created_at, updated_at
                FROM companies
                WHERE domain = %s AND tenant_id = %s
                """,
                (domain, tenant_id),
            )
            return cur.fetchone()
