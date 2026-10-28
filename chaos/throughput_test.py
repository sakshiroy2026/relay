"""Throughput and claim cost (blueprint §13, Test A4): how many runs per second, and
does adding workers actually help?

WHAT IT IS  Two measurements.
            `sweep` — for each fake-model latency and each worker count: enqueue
                      a batch of runs in ONE commit, let the workers drain it,
                      and record runs/s, steps/s, run duration and how evenly
                      the work spread. Every point also re-checks the Day 2
                      claim guarantee (every run claimed exactly once) and the
                      ledgers (check_run), because a fast wrong queue is no use.
                      Latency 0 measures Relay's own overhead (database round
                      trips + Python); a non-zero latency is the realistic case
                      where workers mostly wait on the model.
            `claim` — the claim query itself, timed with EXPLAIN ANALYZE while
                      100,000 finished runs sit in the table, with the partial
                      index and with it dropped. Done inside ONE transaction
                      that is rolled back, so nothing is left behind (it holds a
                      lock on `runs` for a few seconds: run it with no workers up).
GOES IN     python -m chaos.throughput_test [--workers 1,2,4,8,16]
                [--latencies 0,0.2] [--note "..."]
COMES OUT   chaos/throughput_results.json, conditions next to the numbers.
TOUCHES     runs/steps/companies/tool_invocations (the sweep's runs stay, like
            drain_test's); local worker processes.
FAILS WHEN  The database is unreachable. A run claimed twice, a ledger
            problem, or a run that doesn't succeed -> counted, exit code 1.
"""

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from chaos.harness import (
    create_runs,
    percentile,
    query_all,
    spawn_workers,
    stop_workers,
    wait_terminal,
)
from relay.config import settings
from relay.db import pool
from relay.worker import CLAIM_SQL
from scripts.check_run import find_problems

RESULTS = Path(__file__).parent / "throughput_results.json"
BACKLOG_ROWS = 100_000
CLAIM_REPEATS = 25


# ── sweep ────────────────────────────────────────────────────────────────
def batch_size(workers: int, latency: float) -> int:
    """About 10-30 s of work per point, whatever the setting."""
    return 300 if latency == 0 else 30 * workers


def point(workers: int, latency: float) -> dict[str, Any]:
    procs = spawn_workers(workers, {"FAKE_DELAY_SECONDS": str(latency), "LEASE_SECONDS": "30"})
    try:
        time.sleep(2.0)  # imports + first connection, so start-up isn't measured
        n = batch_size(workers, latency)
        tag = uuid.uuid4().hex[:6]
        run_ids = create_runs(
            [f"tput-{tag}-{i}.example" for i in range(n)], written_by="throughput_test"
        )
        statuses = wait_terminal(run_ids, timeout_s=600, poll_s=0.1)
    finally:
        stop_workers(procs)

    ids = [uuid.UUID(r) for r in run_ids]
    runs = query_all(
        "SELECT id, status::text AS status, attempt_count, lease_owner, created_at, "
        "started_at, finished_at FROM runs WHERE id = ANY(%s)",
        (ids,),
    )
    steps = query_all(
        "SELECT run_id, step_index, kind, payload FROM steps WHERE run_id = ANY(%s) "
        "ORDER BY run_id, step_index",
        (ids,),
    )
    by_run: dict[Any, list[dict[str, Any]]] = {}
    for s in steps:
        by_run.setdefault(s["run_id"], []).append(s)

    enqueued = min(r["created_at"] for r in runs)
    last = max(r["finished_at"] for r in runs if r["finished_at"] is not None)
    wall = (last - enqueued).total_seconds()
    durations = [
        (r["finished_at"] - r["started_at"]).total_seconds() for r in runs if r["finished_at"]
    ]
    per_worker = Counter(r["lease_owner"] for r in runs)
    return {
        "workers": workers,
        "fake_model_latency_s": latency,
        "runs": n,
        "statuses": statuses,
        "wall_s": round(wall, 2),
        "runs_per_s": round(n / wall, 2),
        "steps_per_s": round(len(steps) / wall, 1),
        "run_duration_s": {
            "p50": round(statistics.median(durations), 3),
            "p95": round(percentile(durations, 95), 3),
        },
        "runs_claimed_more_than_once": sum(1 for r in runs if r["attempt_count"] != 1),
        "workers_that_did_work": len(per_worker),
        "runs_per_worker_min_max": [min(per_worker.values()), max(per_worker.values())],
        "runs_with_ledger_problems": sum(1 for rs in by_run.values() if find_problems(rs)),
    }


# ── claim query cost ─────────────────────────────────────────────────────
def explain_claim(cur: Any, test_run: uuid.UUID) -> dict[str, Any]:
    times: list[float] = []
    plan_text = ""
    for _ in range(CLAIM_REPEATS):
        cur.execute(
            "UPDATE runs SET status = 'pending', lease_expires_at = NULL, attempt_count = 0 "
            "WHERE id = %s",
            (test_run,),
        )
        cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + CLAIM_SQL, ("throughput_test", 30))
        plan = cur.fetchone()[0][0]
        times.append(float(plan["Execution Time"]))
        plan_text = json.dumps(plan["Plan"])
    return {
        "execution_ms_median": round(statistics.median(times), 3),
        "execution_ms_p90": round(percentile(times, 90), 3),
        "uses_runs_claimable_idx": "runs_claimable_idx" in plan_text,
    }


def claim_cost() -> dict[str, Any]:
    with pool.connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM runs WHERE status IN ('pending', 'running')")
                open_runs = cur.fetchone()[0]  # type: ignore[index]
                if open_runs:
                    return {"skipped": f"{open_runs} open runs in the table; needs an idle queue"}

                cur.execute(
                    "INSERT INTO runs (tenant_id, input, agent_version) "
                    "VALUES (%s, '{}', 'claim-cost') RETURNING id",
                    (settings.dev_tenant_id,),
                )
                test_run = cur.fetchone()[0]  # type: ignore[index]
                cur.execute("SELECT count(*) FROM runs")
                existing = cur.fetchone()[0]  # type: ignore[index]
                cur.execute("ANALYZE runs")
                baseline = explain_claim(cur, test_run)

                cur.execute(
                    """
                    INSERT INTO runs (tenant_id, input, agent_version, status, finished_at)
                    SELECT %s, '{}', 'claim-cost', 'succeeded', now()
                    FROM generate_series(1, %s)
                    """,
                    (settings.dev_tenant_id, BACKLOG_ROWS),
                )
                cur.execute("ANALYZE runs")
                with_index = explain_claim(cur, test_run)

                cur.execute("DROP INDEX runs_claimable_idx")
                cur.execute("ANALYZE runs")
                without_index = explain_claim(cur, test_run)
        finally:
            conn.rollback()  # the 100k rows, the test run and the dropped index all vanish

    return {
        "rows_already_in_table": existing,
        "finished_runs_added": BACKLOG_ROWS,
        "repeats": CLAIM_REPEATS,
        "claim_before_backlog": baseline,
        "claim_with_backlog_partial_index": with_index,
        "claim_with_backlog_index_dropped": without_index,
    }


# ── main ─────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", default="1,2,4,8,16")
    parser.add_argument("--latencies", default="0,0.2")
    parser.add_argument("--note", default="", help="recorded next to the numbers, e.g. which DB")
    args = parser.parse_args()
    worker_counts = [int(x) for x in args.workers.split(",")]
    latencies = [float(x) for x in args.latencies.split(",")]

    started = time.monotonic()
    print("claim query cost ...", flush=True)
    claim = claim_cost()
    print(json.dumps(claim, indent=2), flush=True)

    sweep: list[dict[str, Any]] = []
    for latency in latencies:
        for w in worker_counts:
            p = point(w, latency)
            sweep.append(p)
            print(
                f"latency {latency:>4} s  workers {w:>2}  {p['runs']:>4} runs  "
                f"{p['runs_per_s']:>7} runs/s  p50 {p['run_duration_s']['p50']} s  "
                f"claimed>1: {p['runs_claimed_more_than_once']}  "
                f"ledger problems: {p['runs_with_ledger_problems']}",
                flush=True,
            )

    for latency in latencies:  # scaling efficiency vs the 1-worker point (if measured)
        pts = [p for p in sweep if p["fake_model_latency_s"] == latency]
        one = next((p for p in pts if p["workers"] == 1), None)
        for p in pts:
            if one is not None:
                ideal = one["runs_per_s"] * p["workers"]
                p["speedup_vs_1_worker"] = round(p["runs_per_s"] / one["runs_per_s"], 2)
                p["scaling_efficiency"] = round(p["runs_per_s"] / ideal, 2)

    summary: dict[str, Any] = {
        "conditions": {
            "model": "scripted fake (4 model calls, 4 tool calls per run)",
            "lease_seconds": 30,
            "batch": "runs enqueued in one commit; wall = first created_at to last finished_at",
            "machine_cpus": os.cpu_count(),
        },
        "claim_query": claim,
        "sweep": sweep,
        "elapsed_s": round(time.monotonic() - started, 1),
    }
    if args.note:
        summary["environment_note"] = args.note
    RESULTS.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    pool.close()

    bad = sum(
        p["runs_claimed_more_than_once"]
        + p["runs_with_ledger_problems"]
        + (p["runs"] - p["statuses"].get("succeeded", 0))
        for p in sweep
    )
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
