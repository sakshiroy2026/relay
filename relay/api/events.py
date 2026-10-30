"""Server-sent events for one run: the ledger pushed, not polled (Day 11).

WHAT IT IS  GET /v1/runs/{id}/events streams the same run header and step
            summaries the polling endpoints serve, but pushed the moment a
            ledger row commits: a trigger (migration 006) rings pg_notify on
            every steps insert, and this generator sits in LISTEN until the
            bell rings for its run. A quiet stream still ticks every
            TICK_SECONDS (status/lease changes ring no bell) and sends an SSE
            comment as a keep-alive.
GOES IN     The run id and the authenticated Principal (same auth and rate
            limit as every /v1 route; one stream = one request).
COMES OUT   `event: update` with {run, steps} JSON whenever something changed,
            then `event: done` once the run is terminal, and the stream ends.
            A run the tenant cannot see ends immediately with `event: gone`.
TOUCHES     One dedicated LISTEN connection per open stream (a pool
            connection must not camp in LISTEN), plus pool reads on each wake.
            Fine at demo scale; at real scale one shared listener task would
            fan out to streams — noted in learn/DAY11.md. The LISTEN
            connection skips a PgBouncer pooler (Neon's `-pooler` host):
            transaction pooling drops LISTEN, so it goes to the direct host.
FAILS WHEN  The client disconnects: the generator is closed and the LISTEN
            connection dropped. Database errors end the stream; the page
            falls back to polling.
"""

import json
from collections.abc import Generator
from typing import Any
from uuid import UUID

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from relay.api import repo
from relay.api.routes.runs import summarize_step, unread_sources
from relay.config import settings

TICK_SECONDS = 2.0
TERMINAL = ("succeeded", "failed", "dead")


def listen_conninfo(conninfo: str) -> str:
    """The same database, minus Neon's PgBouncer pooler, for LISTEN.

    A transaction-mode pooler hands each statement to whichever server
    connection is free, so a LISTEN is forgotten and no notification ever
    arrives. Neon names the pooled host `<endpoint>-pooler.<region>...` and the
    direct one `<endpoint>.<region>...`; any other host is returned unchanged.
    """
    params = conninfo_to_dict(conninfo)
    host = params.get("host")
    if isinstance(host, str) and "-pooler." in host:
        params["host"] = host.replace("-pooler.", ".", 1)
        return make_conninfo("", **params)
    return conninfo


def _snapshot(tenant_id: UUID, run_id: UUID) -> tuple[dict[str, Any] | None, list[dict], str]:
    run = repo.get_run(tenant_id, run_id)
    if run is None:
        return None, [], ""
    rows = repo.get_run_steps(tenant_id, run_id)
    run["unread_sources"] = unread_sources(run, rows)
    steps = [
        {
            "step_index": r["step_index"],
            "kind": r["kind"],
            "written_by": r["written_by"],
            "created_at": r["created_at"],
            "summary": summarize_step(r["kind"], r["payload"]),
        }
        for r in rows
    ]
    # what "changed" means: a new step, a status flip, a takeover, or spend
    fingerprint = f"{len(steps)}|{run['status']}|{run['lease_owner']}|{run['spent_usd']}"
    return run, steps, fingerprint


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def run_event_stream(tenant_id: UUID, run_id: UUID) -> Generator[str]:
    last = ""
    # A dedicated autocommit connection: LISTEN state is per-connection, and a
    # pooled connection must go back clean. pg_notify fires on commit, so an
    # event can never describe a row that later rolled back.
    with psycopg.connect(listen_conninfo(settings.database_url), autocommit=True) as conn:
        conn.execute("LISTEN relay_steps")
        while True:
            run, steps, fingerprint = _snapshot(tenant_id, run_id)
            if run is None:
                yield _sse("gone", {"detail": "run not found"})
                return
            if fingerprint != last:
                last = fingerprint
                yield _sse("update", {"run": run, "steps": steps})
                if run["status"] in TERMINAL:
                    yield _sse("done", {"status": run["status"]})
                    return
            else:
                yield ": tick\n\n"  # SSE comment: keeps proxies from cutting a quiet stream

            # Sleep until OUR run's bell rings, or the tick passes. Other runs'
            # bells wake us too (one channel); the fingerprint makes that a no-op.
            for notice in conn.notifies(timeout=TICK_SECONDS, stop_after=1):
                if notice.payload != str(run_id):
                    break  # not ours: re-check (cheap) rather than drain the queue
