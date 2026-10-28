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


class IdempotencyMismatch(Exception):
    """The Idempotency-Key was already used by this tenant for a different request body."""


def request_hash(body: dict[str, Any]) -> str:
    """Same body -> same hash, whatever the key order the client sent."""
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


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
) -> dict[str, Any]:
    body_hash = request_hash({"domain": domain})  # what the CLIENT sent; the mode is ours
    payload = {"domain": domain, "mode": run_mode}  # the run keeps its mode for life (P1)
    # live runs spend real money: they get the tight live budget, demo runs the default
    budget = settings.live_run_budget_usd if run_mode == "live" else None
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
                    return {"run_id": seen[1], "status": seen[2], "replayed": True}

            cur.execute(
                "SELECT count(*) FROM runs "
                "WHERE tenant_id = %s AND status IN ('pending', 'running')",
                (tenant_id,),
            )
            row = cur.fetchone()
            open_runs = 0 if row is None else int(row[0])
            if open_runs >= max_concurrent:
                raise ConcurrencyLimit(open_runs, max_concurrent)  # rolls the lock back too

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
    return {"run_id": created[0], "status": created[1], "replayed": False}


def get_run(tenant_id: UUID, run_id: UUID) -> dict[str, Any] | None:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, status, input, result, error,
                       attempt_count, next_step_index, lease_owner,
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
