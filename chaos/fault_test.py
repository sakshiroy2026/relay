"""Fault injection (blueprint §13, Test A5): flaky tools, a dead tool, and the breaker.

WHAT IT IS  Turns "it retries and short-circuits" into numbers. Two scenarios:
            `flaky`  — every fake external tool call fails 30% of the time and
                       the fake model 10%; measures retries and recoveries.
            `outage` — fetch_page is DOWN for phase 1 (the breaker must open),
                       then healthy again for phase 2 (the breaker must close
                       by itself via one probe).
GOES IN     python -m chaos.fault_test --scenario flaky [--n 50] [--workers 3]
            python -m chaos.fault_test --scenario outage
            Needs a reachable database with migrations 001-003. No Docker and
            no API: workers are spawned as local `python -m relay.worker`
            processes with the fault knobs in their environment, and runs are
            created with the same two inserts the API does.
COMES OUT   chaos/fault_results.json — the scenario's summary is merged in
            under its own key, with the conditions written next to the numbers.
TOUCHES     runs/steps/companies/tool_invocations/circuit_breakers. Resets the
            two external tools' breaker rows at the start of each scenario.
FAILS WHEN  The database is unreachable, or a run never reaches a terminal
            state before the timeout -> reported in the summary, not hidden.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from relay.config import settings
from relay.db import pool
from scripts.check_run import check_run

HERE = Path(__file__).parent
RESULTS = HERE / "fault_results.json"
RUN_TIMEOUT_SECONDS = 120


# ── database helpers ─────────────────────────────────────────────────────
def create_run(domain: str) -> str:
    """The API's two inserts (run + step 0), without needing the API up."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (tenant_id, input, agent_version) VALUES (%s, %s, %s) "
                "RETURNING id",
                (settings.dev_tenant_id, Jsonb({"domain": domain}), "enrich@v2"),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("run insert returned no row")
            run_id = row[0]
            cur.execute(
                "INSERT INTO steps (run_id, step_index, kind, payload, written_by) "
                "VALUES (%s, 0, 'run_started', %s, %s)",
                (run_id, Jsonb({"domain": domain}), "fault_test"),
            )
    return str(run_id)


def query_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return [dict(r) for r in cur.execute(sql, params).fetchall()]


def reset_breakers() -> None:
    with pool.connection() as conn:
        conn.execute("DELETE FROM circuit_breakers WHERE tool IN ('web_search', 'fetch_page')")


def breaker_states() -> dict[str, str]:
    rows = query_all("SELECT tool, state FROM circuit_breakers ORDER BY tool")
    return {r["tool"]: r["state"] for r in rows}


def wait_terminal(run_ids: list[str]) -> dict[str, int]:
    """Block until every run is terminal (or the timeout passes). Returns status counts."""
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    while True:
        rows = query_all(
            "SELECT status::text AS status, count(*) AS n FROM runs WHERE id = ANY(%s) "
            "GROUP BY status",
            ([uuid.UUID(r) for r in run_ids],),
        )
        counts = {r["status"]: r["n"] for r in rows}
        open_runs = counts.get("pending", 0) + counts.get("running", 0)
        if open_runs == 0 or time.monotonic() > deadline:
            if open_runs:
                counts["TIMED_OUT_NON_TERMINAL"] = open_runs
            return counts
        time.sleep(0.5)


# ── workers as local processes ───────────────────────────────────────────
def spawn_workers(n: int, extra_env: dict[str, str]) -> list[subprocess.Popen[bytes]]:
    env = {**os.environ, **extra_env}
    return [
        subprocess.Popen(
            [sys.executable, "-m", "relay.worker"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(n)
    ]


def stop_workers(procs: list[subprocess.Popen[bytes]]) -> None:
    for p in procs:
        p.kill()
    for p in procs:
        p.wait(timeout=10)


# ── ledger scanning ──────────────────────────────────────────────────────
def scan(run_ids: list[str]) -> dict[str, Any]:
    ids = [uuid.UUID(r) for r in run_ids]
    tool_results = query_all(
        "SELECT payload FROM steps WHERE run_id = ANY(%s) AND kind = 'tool_result'", (ids,)
    )
    model_calls = query_all(
        "SELECT payload FROM steps WHERE run_id = ANY(%s) AND kind = 'model_call'", (ids,)
    )
    errors = query_all(
        "SELECT payload FROM steps WHERE run_id = ANY(%s) AND kind = 'error'", (ids,)
    )

    attempts_hist: dict[str, int] = {}
    recovered = exhausted = short_circuited = 0
    for r in tool_results:
        p = r["payload"]
        a = int(p.get("attempts", 1))
        attempts_hist[str(a)] = attempts_hist.get(str(a), 0) + 1
        if p.get("breaker_open"):
            short_circuited += 1
        elif p["ok"] and a > 1:
            recovered += 1
        elif not p["ok"] and "failed after" in p["content"]:
            exhausted += 1

    problems = {rid: check_run(rid) for rid in run_ids}
    return {
        "tool_results": len(tool_results),
        "tool_attempts_histogram": dict(sorted(attempts_hist.items())),
        "tool_calls_recovered_by_retry": recovered,
        "tool_calls_exhausted_retries": exhausted,
        "tool_calls_short_circuited_by_breaker": short_circuited,
        "model_calls": len(model_calls),
        "model_calls_retried": sum(1 for m in model_calls if m["payload"].get("attempts", 1) > 1),
        "model_unavailable_errors": sum(
            1 for e in errors if e["payload"].get("reason") == "model_unavailable"
        ),
        "ledger_problems": {k: v for k, v in problems.items() if v},  # empty dict = all clean
    }


# ── scenarios ────────────────────────────────────────────────────────────
def scenario_flaky(n: int, workers: int) -> dict[str, Any]:
    reset_breakers()
    rate_tool, rate_model = "0.3", "0.1"
    procs = spawn_workers(
        workers,
        {"FAKE_TOOL_FAILURE_RATE": rate_tool, "FAKE_MODEL_FAILURE_RATE": rate_model},
    )
    try:
        run_ids = [create_run(f"fault-{i}-{uuid.uuid4().hex[:4]}.example") for i in range(n)]
        statuses = wait_terminal(run_ids)
    finally:
        stop_workers(procs)

    return {
        "conditions": {
            "runs": n,
            "workers": workers,
            "fake_tool_failure_rate": float(rate_tool),
            "fake_model_failure_rate": float(rate_model),
            "retry_attempts_per_call": 3,
        },
        "statuses": statuses,
        **scan(run_ids),
        "breaker_states_at_end": breaker_states(),
    }


def scenario_outage(workers: int) -> dict[str, Any]:
    reset_breakers()
    open_seconds = "8"  # shorter than the default 30 so the proof doesn't crawl

    # Phase 1: fetch_page is down. The breaker must open and stop the hammering.
    procs = spawn_workers(
        workers, {"FAKE_TOOL_DOWN": "fetch_page", "BREAKER_OPEN_SECONDS": open_seconds}
    )
    try:
        down_ids = [create_run(f"outage-{i}-{uuid.uuid4().hex[:4]}.example") for i in range(8)]
        down_statuses = wait_terminal(down_ids)
    finally:
        stop_workers(procs)
    phase1 = {"statuses": down_statuses, **scan(down_ids), "breaker": breaker_states()}

    # Phase 2: the outage is over. After open_seconds, one probe must close the breaker.
    time.sleep(float(open_seconds) + 1)
    procs = spawn_workers(workers, {"BREAKER_OPEN_SECONDS": open_seconds})
    try:
        up_ids = [create_run(f"recover-{i}-{uuid.uuid4().hex[:4]}.example") for i in range(3)]
        up_statuses = wait_terminal(up_ids)
    finally:
        stop_workers(procs)
    phase2 = {"statuses": up_statuses, **scan(up_ids), "breaker": breaker_states()}

    return {
        "conditions": {
            "down_tool": "fetch_page",
            "breaker_threshold": settings.breaker_threshold,
            "breaker_open_seconds_used": float(open_seconds),
            "workers": workers,
            "note": (
                "the scripted fake model carries on whatever the tools return, so run "
                "SUCCESS here only proves runs don't crash; the breaker numbers are the point"
            ),
        },
        "phase1_tool_down": phase1,
        "phase2_recovered": phase2,
    }


# ── main ─────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["flaky", "outage"], required=True)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--note", default="", help="recorded next to the numbers, e.g. which DB")
    args = parser.parse_args()

    started = time.monotonic()
    if args.scenario == "flaky":
        summary = scenario_flaky(args.n, args.workers)
    else:
        summary = scenario_outage(args.workers)
    summary["elapsed_s"] = round(time.monotonic() - started, 1)
    if args.note:
        summary["environment_note"] = args.note

    merged = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}
    merged[args.scenario] = summary
    RESULTS.write_text(json.dumps(merged, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({args.scenario: summary}, indent=2, default=str))
    pool.close()


if __name__ == "__main__":
    main()
