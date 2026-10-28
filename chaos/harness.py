"""Shared plumbing for the Day 16 harnesses (idempotency, fencing, throughput).

WHAT IT IS  The bits every harness repeats: create runs without the API, spawn
            and stop local worker processes, poll for terminal runs, read the
            database clock. Like chaos/fault_test.py, no Docker and no API.
GOES IN     Run domains, worker counts, extra environment for the workers
            (LEASE_SECONDS, FAKE_DELAY_SECONDS, ...), an optional log directory.
COMES OUT   Run ids, Worker handles (process + log file + worker id), status
            counts.
TOUCHES     runs/steps (inserts), local processes.
FAILS WHEN  The database is unreachable -> psycopg errors propagate.
"""

import os
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from relay.config import settings
from relay.db import pool

AGENT_VERSION = "enrich@v2"  # what the API stamps, so these runs look like real ones


# ── database helpers ─────────────────────────────────────────────────────
def create_runs(domains: list[str], written_by: str, status: str = "pending") -> list[str]:
    """The API's two inserts per run (run + step 0), all in ONE commit.

    One commit means every run becomes claimable at the same instant, which is
    what a throughput test needs. status='dead' parks a run no worker will
    ever claim (for tests that call the tools directly).
    """
    ids: list[str] = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for domain in domains:
                cur.execute(
                    "INSERT INTO runs (tenant_id, input, agent_version, status) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (settings.dev_tenant_id, Jsonb({"domain": domain}), AGENT_VERSION, status),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("run insert returned no row")
                cur.execute(
                    "INSERT INTO steps (run_id, step_index, kind, payload, written_by) "
                    "VALUES (%s, 0, 'run_started', %s, %s)",
                    (row[0], Jsonb({"domain": domain}), written_by),
                )
                ids.append(str(row[0]))
    return ids


def query_all(sql: str, params: Any = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return [dict(r) for r in cur.execute(sql, params).fetchall()]


def query_one(sql: str, params: Any = ()) -> dict[str, Any]:
    rows = query_all(sql, params)
    if not rows:
        raise RuntimeError(f"no row for {sql[:50]}…")
    return rows[0]


def db_now() -> datetime:
    """The database clock: steps.created_at uses it, so comparisons must too."""
    return query_one("SELECT now() AS t")["t"]


def wait_terminal(run_ids: list[str], timeout_s: float, poll_s: float = 0.2) -> dict[str, int]:
    """Block until every run is terminal (or the timeout passes). Returns status counts."""
    ids = [uuid.UUID(r) for r in run_ids]
    deadline = time.monotonic() + timeout_s
    while True:
        rows = query_all(
            "SELECT status::text AS status, count(*) AS n FROM runs WHERE id = ANY(%s) "
            "GROUP BY status",
            (ids,),
        )
        counts = {r["status"]: r["n"] for r in rows}
        open_runs = counts.get("pending", 0) + counts.get("running", 0)
        if open_runs == 0 or time.monotonic() > deadline:
            if open_runs:
                counts["TIMED_OUT_NON_TERMINAL"] = open_runs
            return counts
        time.sleep(poll_s)


# ── workers as local processes ───────────────────────────────────────────
@dataclass
class Worker:
    proc: subprocess.Popen[bytes]
    log: Path | None

    @property
    def worker_id(self) -> str:
        # relay/worker.py: WORKER_ID = f"{hostname}:{pid}"; same host, so the pid is enough
        return f"{socket.gethostname()}:{self.proc.pid}"

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace") if self.log else ""


def spawn_workers(n: int, extra_env: dict[str, str], log_dir: Path | None = None) -> list[Worker]:
    env = {**os.environ, **extra_env}
    workers: list[Worker] = []
    for i in range(n):
        log = None if log_dir is None else log_dir / f"worker-{i}.log"
        out = subprocess.DEVNULL if log is None else log.open("wb")
        proc = subprocess.Popen(
            [sys.executable, "-m", "relay.worker"], env=env, stdout=out, stderr=subprocess.STDOUT
        )
        workers.append(Worker(proc, log))
    return workers


def stop_workers(workers: list[Worker]) -> None:
    for w in workers:
        w.proc.kill()
    for w in workers:
        w.proc.wait(timeout=10)


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile; q in [0, 100]. Good enough for a results table."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(q / 100 * len(ordered) + 0.5) - 1))
    return ordered[k]
