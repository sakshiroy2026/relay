"""Idempotency under pressure (blueprint §13, Test A2): racers and kills on save_company.

WHAT IT IS  Turns "a write tool commits at most once per key" into numbers, with
            far more pressure than a crashed run ever produces. Three phases:
            `race`         — R processes call save_company with the SAME keys at
                             the same moment, through the real dispatch_tool path.
            `race_widened` — the same, but each write holds its transaction open
                             for --widen-ms first, so racers are guaranteed to
                             overlap and the "loser rolls back" path is forced.
            `kill`         — one process per key, kill -9'd at a random moment
                             while its write holds the transaction open; then the
                             key is re-issued once, like the next worker would.
GOES IN     python -m chaos.idempotency_test [--keys 300] [--racers 4]
                [--widen-ms 20] [--kill-keys 200] [--note "..."]
            Needs a database with migrations 001-003. Runs are parked
            (status 'dead'), so live workers never touch them.
COMES OUT   chaos/idempotency_results.json, conditions next to the numbers.
TOUCHES     runs/steps (parked runs), companies, tool_invocations.
FAILS WHEN  The database is unreachable. Any key executed twice, any company
            row without its receipt (or the reverse) -> reported, exit code 1.
"""

import argparse
import json
import random
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg

from chaos.harness import create_runs, query_all
from relay.db import pool

HERE = Path(__file__).parent
RESULTS = HERE / "idempotency_results.json"

Job = tuple[str, str, str]  # (run_id, domain, call_id) -> key '<run_id>:<call_id>'


# ── the racer: a separate process, so racers are real concurrent clients ──
def racer(jobs: list[Job], widen_ms: int) -> None:
    """Wait for 'go' on stdin, then save every job once. One JSON line per job.

    The production path is used unchanged (dispatch_tool -> _save_company ->
    run_once); the only addition is a wrapper that counts how often the write
    itself ran (`executes`), and optionally sleeps inside the transaction first.
    """
    import relay.agent.tools as tools
    from relay.agent.fake_script import ENRICH_SCRIPT
    from relay.agent.tools import ToolContext, dispatch_tool

    record = ENRICH_SCRIPT[2].tool_calls[0].args
    real_run_once = tools.run_once
    executes = 0

    def counting_run_once(key: str, run_id: Any, idx: int, name: str, execute: Any) -> Any:
        def wrapped(cur: psycopg.Cursor[Any]) -> dict[str, Any]:
            nonlocal executes
            executes += 1
            if widen_ms:
                cur.execute("SELECT pg_sleep(%s)", (widen_ms / 1000,))
            result: dict[str, Any] = execute(cur)
            return result

        return real_run_once(key, run_id, idx, name, wrapped)

    tools.run_once = counting_run_once  # type: ignore[assignment]

    print("ready", flush=True)
    sys.stdin.readline()  # the barrier: every racer starts on the same 'go'
    for run_id, domain, call_id in jobs:
        before = executes
        ctx = ToolContext(
            run_id=uuid.UUID(run_id), domain=domain, step_index=1, idem_key=f"{run_id}:{call_id}"
        )
        out = dispatch_tool("save_company", record, ctx)
        line = {"key": ctx.idem_key, "ok": out.ok, "content": out.content}
        print(json.dumps({**line, "executes": executes - before}), flush=True)
    pool.close()


def spawn_racer(jobs: list[Job], widen_ms: int) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "chaos.idempotency_test", "--racer", json.dumps(jobs)]
        + ["--widen-ms", str(widen_ms)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    if proc.stdout.readline().strip() != "ready":
        raise RuntimeError("racer did not start")
    return proc


def go(proc: subprocess.Popen[str]) -> None:
    assert proc.stdin is not None
    proc.stdin.write("go\n")
    proc.stdin.flush()


# ── what the database says ───────────────────────────────────────────────
def receipts(run_ids: list[str]) -> dict[str, dict[str, Any]]:
    rows = query_all(
        "SELECT idem_key, status, attempts, executions FROM tool_invocations "
        "WHERE run_id = ANY(%s)",
        ([uuid.UUID(r) for r in run_ids],),
    )
    return {r["idem_key"]: r for r in rows}


def companies(domains: list[str]) -> Counter[str]:
    rows = query_all("SELECT domain FROM companies WHERE domain = ANY(%s)", (domains,))
    return Counter(r["domain"] for r in rows)


def new_jobs(n: int, prefix: str) -> tuple[list[Job], list[str], list[str]]:
    domains = [f"{prefix}-{i}-{uuid.uuid4().hex[:6]}.example" for i in range(n)]
    run_ids = create_runs(domains, written_by="idempotency_test", status="dead")
    jobs = [(rid, d, "call_save") for rid, d in zip(run_ids, domains, strict=True)]
    return jobs, run_ids, domains


# ── phases ───────────────────────────────────────────────────────────────
def phase_race(keys: int, racers: int, widen_ms: int) -> dict[str, Any]:
    jobs, run_ids, domains = new_jobs(keys, f"race{widen_ms}")
    procs = [spawn_racer(jobs, widen_ms) for _ in range(racers)]
    started = time.monotonic()
    for p in procs:
        go(p)
    outputs = [p.communicate(timeout=600)[0] for p in procs]
    elapsed = time.monotonic() - started

    by_key: dict[str, list[dict[str, Any]]] = {}
    for out in outputs:
        for line in out.splitlines():
            row = json.loads(line)
            by_key.setdefault(row["key"], []).append(row)

    rec = receipts(run_ids)
    rows = companies(domains)
    executes = sum(r["executes"] for rs in by_key.values() for r in rs)
    committed = sum(r["executions"] for r in rec.values())
    return {
        "conditions": {"keys": keys, "racers_per_key": racers, "widen_ms": widen_ms},
        "calls": sum(len(rs) for rs in by_key.values()),
        "keys_executed_exactly_once": sum(1 for r in rec.values() if r["executions"] == 1),
        "keys_executed_more_than_once": sum(1 for r in rec.values() if r["executions"] > 1),
        "keys_never_executed": keys - sum(1 for r in rec.values() if r["executions"] >= 1),
        "keys_where_callers_got_different_answers": sum(
            # compared parsed: jsonb stores keys in its own order, so the winner's
            # fresh string and the stored receipt differ in key order, not in value
            1
            for rs in by_key.values()
            if len({json.dumps(json.loads(r["content"]), sort_keys=True) for r in rs}) > 1
        ),
        "calls_not_ok": sum(1 for rs in by_key.values() for r in rs if not r["ok"]),
        "writes_started": executes,
        "writes_committed": committed,
        "writes_rolled_back_by_the_receipt_check": executes - committed,
        "calls_answered_from_the_receipt": sum(len(rs) for rs in by_key.values()) - executes,
        "attempts_recorded_per_key": dict(Counter(r["attempts"] for r in rec.values())),
        "domains_with_company_rows_not_exactly_1": sum(1 for d in domains if rows[d] != 1),
        "elapsed_s": round(elapsed, 2),
    }


def phase_kill(keys: int, widen_ms: int, rng: random.Random) -> dict[str, Any]:
    jobs, run_ids, domains = new_jobs(keys, "kill")
    landed: Counter[str] = Counter()
    atomicity_breaks: list[str] = []
    finished_before_kill = 0
    window = widen_ms / 1000 * 1.5  # kill moment: uniform over the write window and a bit after

    for batch_start in range(0, keys, 20):  # 20 racers alive at a time
        batch = jobs[batch_start : batch_start + 20]
        procs = [spawn_racer([job], widen_ms) for job in batch]
        for p in procs:
            go(p)
        t0 = time.monotonic()
        delays = sorted((rng.uniform(0, window), i) for i in range(len(procs)))
        for delay, i in delays:
            time.sleep(max(0.0, t0 + delay - time.monotonic()))
            if procs[i].poll() is not None:
                finished_before_kill += 1
            procs[i].kill()  # SIGKILL: no cleanup, the socket just closes
        for p in procs:
            p.wait(timeout=10)

    time.sleep(0.5)  # let Postgres notice the dead sockets and roll their transactions back
    rec_after_kill = receipts(run_ids)
    rows_after_kill = companies(domains)
    for run_id, domain, call_id in jobs:
        r = rec_after_kill.get(f"{run_id}:{call_id}")
        status = "no receipt yet (killed before step 1)" if r is None else r["status"]
        landed[status] += 1
        has_row = rows_after_kill[domain] == 1
        if (r is not None and r["status"] == "ok") != has_row:  # write and receipt disagree
            atomicity_breaks.append(f"{domain}: receipt={status} company_rows={has_row}")

    # The next worker re-issues every key once (no widening: the normal path).
    reissue = spawn_racer(jobs, 0)
    go(reissue)
    reissue_out = [json.loads(x) for x in reissue.communicate(timeout=600)[0].splitlines()]

    rec = receipts(run_ids)
    rows = companies(domains)
    return {
        "conditions": {
            "keys": keys,
            "widen_ms": widen_ms,
            "kill": "SIGKILL at a uniform random moment in [0, 1.5 x widen_ms] after 'go'",
        },
        "where_the_kill_landed": {
            "receipt in_flight (killed mid-write; the write must have rolled back)": landed.get(
                "in_flight", 0
            ),
            "receipt ok (the write had committed before the kill)": landed.get("ok", 0),
            "no receipt yet (killed before step 1)": landed.get(
                "no receipt yet (killed before step 1)", 0
            ),
            "(of those ok: the process had already exited)": finished_before_kill,
        },
        "write_and_receipt_disagree_after_kill": len(atomicity_breaks),
        "atomicity_break_examples": atomicity_breaks[:5],
        "reissue_calls_not_ok": sum(1 for r in reissue_out if not r["ok"]),
        "reissues_that_executed_the_write": sum(r["executes"] for r in reissue_out),
        "keys_executed_exactly_once_after_reissue": sum(
            1 for r in rec.values() if r["executions"] == 1
        ),
        "keys_executed_more_than_once": sum(1 for r in rec.values() if r["executions"] > 1),
        "domains_with_company_rows_not_exactly_1": sum(1 for d in domains if rows[d] != 1),
    }


# ── main ─────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys", type=int, default=300)
    parser.add_argument("--racers", type=int, default=4)
    parser.add_argument("--widen-ms", type=int, default=20)
    parser.add_argument("--kill-keys", type=int, default=200)
    parser.add_argument("--kill-widen-ms", type=int, default=200)
    parser.add_argument("--seed", type=int, default=16)
    parser.add_argument("--note", default="", help="recorded next to the numbers, e.g. which DB")
    parser.add_argument("--racer", help=argparse.SUPPRESS)  # internal: JSON list of jobs
    args = parser.parse_args()

    if args.racer is not None:
        racer([tuple(j) for j in json.loads(args.racer)], args.widen_ms)
        return

    started = time.monotonic()
    phases: dict[str, Callable[[], dict[str, Any]]] = {
        "race": lambda: phase_race(args.keys, args.racers, 0),
        "race_widened": lambda: phase_race(args.keys, args.racers, args.widen_ms),
        "kill": lambda: phase_kill(args.kill_keys, args.kill_widen_ms, random.Random(args.seed)),
    }
    summary: dict[str, Any] = {}
    for name, run in phases.items():
        print(f"phase {name} ...", flush=True)
        summary[name] = run()
    summary["elapsed_s"] = round(time.monotonic() - started, 1)
    if args.note:
        summary["environment_note"] = args.note

    RESULTS.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str))
    pool.close()

    bad = (
        sum(summary[p]["keys_executed_more_than_once"] for p in phases)
        + sum(summary[p]["domains_with_company_rows_not_exactly_1"] for p in phases)
        + summary["kill"]["write_and_receipt_disagree_after_kill"]
    )
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
