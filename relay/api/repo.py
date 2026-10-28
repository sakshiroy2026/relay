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
TOUCHES     runs, steps, companies.
FAILS WHEN  Database errors propagate. create_run raises ConcurrencyLimit
            when the tenant's open runs are at max_concurrent_runs: the count
            and the insert happen under a per-tenant advisory transaction
            lock, so two racing requests cannot both squeeze past the cap.
"""

from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from relay.db import pool

AGENT_VERSION = "enrich@v2"


class ConcurrencyLimit(Exception):
    """The tenant already has max_concurrent_runs runs pending or running."""

    def __init__(self, open_runs: int, cap: int) -> None:
        self.open_runs, self.cap = open_runs, cap
        super().__init__(f"{open_runs} open runs, cap {cap}")


def create_run(tenant_id: UUID, domain: str, max_concurrent: int) -> dict[str, Any]:
    payload = {"domain": domain}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # One advisory lock per tenant for the length of this transaction:
            # concurrent creates for the SAME tenant queue up for a few ms, so
            # the count below can't be stale when the insert commits. Other
            # tenants use other lock keys and are not slowed down.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 42))", (tenant_id,)
            )
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
                "INSERT INTO runs (tenant_id, input, agent_version) VALUES (%s, %s, %s) "
                "RETURNING id, status",
                (tenant_id, Jsonb(payload), AGENT_VERSION),
            )
            created = cur.fetchone()
            if created is None:
                raise RuntimeError("run insert returned no row")
            cur.execute(
                "INSERT INTO steps (run_id, step_index, kind, payload, written_by) "
                "VALUES (%s, 0, 'run_started', %s, 'api')",
                (created[0], Jsonb(payload)),
            )
    return {"run_id": created[0], "status": created[1]}


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
