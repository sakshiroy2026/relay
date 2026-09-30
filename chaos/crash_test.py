"""Chaos harness (blueprint §13, Test A1): start runs, kill their worker at a random moment.

WHAT IT IS  Turns "it survives crashes" into numbers. For each iteration: POST a run
            with a unique domain, wait a random time, `docker kill` the container
            that owns the run, wait for the run to finish, restart the worker, and
            record what the ledger says.
GOES IN     python -m chaos.crash_test [--n 200] [--summary]
            Needs: the API on 127.0.0.1:8001, the 3 worker containers, and in .env
            RELAY_API_KEY, LEASE_SECONDS and FAKE_DELAY_SECONDS (both are recorded).
COMES OUT   chaos/iterations.jsonl: one line per finished iteration, appended as it
            goes, so a stopped harness resumes where it left off (same command).
            chaos/results.json: the summary (written at the end, or with --summary).
TOUCHES     runs/steps/companies/tool_invocations (reads; runs are created via the
            API), Docker (kill + restart workers). Never prints the API key.
FAILS WHEN  The API or Docker is unreachable -> the error is printed and the
            harness stops; rerun the same command to resume.
"""

import argparse
import json
import random
import statistics
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from relay.config import settings
from relay.db import pool
from scripts.check_run import check_run

HERE = Path(__file__).parent
ITERATIONS = HERE / "iterations.jsonl"
RESULTS = HERE / "results.json"
API = "http://127.0.0.1:8001"
KILL_WINDOW_SECONDS = (0.0, 10.0)  # a normal run takes ~9 s at FAKE_DELAY_SECONDS=1.5
RUN_TIMEOUT_SECONDS = 150
COMPOSE = ["docker", "compose", "-f", str(HERE.parent / "docker-compose.yml")]


# ── helpers ──────────────────────────────────────────────────────────────
def post_run(domain: str) -> str:
    req = urllib.request.Request(
        f"{API}/v1/runs",
        data=json.dumps({"domain": domain}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.relay_api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return str(json.loads(resp.read())["run_id"])


def query_one(sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(sql, params).fetchone()
    if row is None:
        raise RuntimeError(f"no row for {sql[:40]}…")
    return dict(row)


def query_all(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return [dict(r) for r in cur.execute(sql, params).fetchall()]


def containers_by_id() -> dict[str, str]:
    out = subprocess.run(
        ["docker", "ps", "--format", "{{.ID}} {{.Names}}"], capture_output=True, text=True
    ).stdout
    return {line.split()[0]: line.split()[1] for line in out.splitlines() if line.strip()}


def ensure_workers() -> None:
    subprocess.run([*COMPOSE, "up", "-d", "worker"], capture_output=True, check=True)
    for _ in range(30):
        if sum(1 for n in containers_by_id().values() if "worker" in n) >= 3:
            return
        time.sleep(1)
    raise RuntimeError("fewer than 3 worker containers running")


def wait_terminal(run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    while True:
        run = query_one("SELECT status, attempt_count FROM runs WHERE id = %s", (run_id,))
        if run["status"] in ("succeeded", "failed", "dead") or time.monotonic() > deadline:
            return run
        time.sleep(0.5)


# ── one iteration ────────────────────────────────────────────────────────
def iteration(i: int) -> dict[str, Any]:
    domain = f"chaos-{i}-{uuid.uuid4().hex[:4]}.example"
    started = time.monotonic()
    run_id = post_run(domain)
    planned = random.uniform(*KILL_WINDOW_SECONDS)
    time.sleep(planned)

    rec: dict[str, Any] = {"i": i, "domain": domain, "run_id": run_id, "planned_kill_s": planned}
    run = query_one("SELECT status, lease_owner FROM runs WHERE id = %s", (run_id,))

    kill_at = None
    if run["status"] == "pending" or not run["lease_owner"]:
        rec["outcome"] = "no_kill_before_claim"
    elif run["status"] != "running":
        rec["outcome"] = "no_kill_after_finish"
    else:
        container = containers_by_id().get(run["lease_owner"].split(":")[0])
        if container is None:
            raise RuntimeError(f"owner {run['lease_owner']} is not a running container")
        subprocess.run(["docker", "kill", container], capture_output=True, check=True)
        kill_at = query_one("SELECT now() AS t", ())["t"]  # database clock, like steps.created_at
        rec |= {"outcome": "killed", "killed": container, "kill_at": kill_at.isoformat()}

    final = wait_terminal(run_id)
    rec |= {"status": final["status"], "attempts": final["attempt_count"]}

    steps = query_all(
        "SELECT step_index, kind, payload, written_by, created_at FROM steps "
        "WHERE run_id = %s ORDER BY step_index",
        (run_id,),
    )
    model_calls = [s for s in steps if s["kind"] == "model_call"]
    tool_calls = [s for s in steps if s["kind"] == "tool_call"]

    def tokens(rows: list[dict[str, Any]]) -> int:
        return sum(r["payload"]["input_tokens"] + r["payload"]["output_tokens"] for r in rows)

    rec |= {
        "rows": len(steps),
        "model_calls": len(model_calls),
        "tokens_total": tokens(model_calls),
        # a tool call written again after a crash (Day 6 edge case 1)
        "rerun_tool_calls": len(tool_calls)
        - len({s["payload"]["tool_call_id"] for s in tool_calls}),
        "check_problems": check_run(run_id),
    }

    if kill_at is not None:
        pre = [s for s in steps if s["created_at"] <= kill_at]
        post = [s for s in steps if s["created_at"] > kill_at]
        rec |= {
            "pre_kill_rows": len(pre),
            "pre_kill_last_kind": pre[-1]["kind"] if pre else None,
            "pre_kill_model_calls": sum(1 for s in pre if s["kind"] == "model_call"),
            "pre_kill_tokens": tokens([s for s in pre if s["kind"] == "model_call"]),
            "resume_s": (post[0]["created_at"] - kill_at).total_seconds() if post else None,
            "resumed_by": post[0]["written_by"] if post else None,
        }

    company = query_one("SELECT count(*) AS n FROM companies WHERE domain = %s", (domain,))
    save = query_all(
        "SELECT attempts, executions FROM tool_invocations "
        "WHERE run_id = %s AND tool_name = 'save_company'",
        (run_id,),
    )
    rec |= {
        "company_rows": company["n"],
        "save_attempts": sum(s["attempts"] for s in save),
        "save_executions_max": max((s["executions"] for s in save), default=0),
    }

    ensure_workers()  # bring the killed container back for the next iteration
    rec["elapsed_s"] = round(time.monotonic() - started, 1)
    return rec


# ── summary ──────────────────────────────────────────────────────────────
def summarize() -> dict[str, Any]:
    recs = [json.loads(line) for line in ITERATIONS.read_text(encoding="utf-8").splitlines()]
    killed = [r for r in recs if r["outcome"] == "killed"]
    resumed = [r["resume_s"] for r in killed if r.get("resume_s") is not None]
    pre_tokens = sum(r["pre_kill_tokens"] for r in killed)
    total_tokens_killed = sum(r["tokens_total"] for r in killed)

    # database-wide checks over every chaos domain, not just the recorded ones
    dup_companies = query_all(
        "SELECT domain, count(*) AS n FROM companies WHERE domain LIKE 'chaos-%%' "
        "GROUP BY domain HAVING count(*) > 1",
        (),
    )
    dup_exec = query_all(
        "SELECT ti.idem_key, ti.executions FROM tool_invocations ti "
        "JOIN runs r ON r.id = ti.run_id "
        "WHERE r.input->>'domain' LIKE 'chaos-%%' AND ti.executions > 1",
        (),
    )

    return {
        "setup": {
            "model": "fake (scripted), tools: fake search/fetch + real Postgres writes",
            "fake_delay_seconds": settings.fake_delay_seconds,
            "lease_seconds": settings.lease_seconds,
            "workers": 3,
            "kill_window_seconds": list(KILL_WINDOW_SECONDS),
        },
        "iterations": len(recs),
        "killed_mid_run": len(killed),
        "no_kill_before_claim": sum(r["outcome"] == "no_kill_before_claim" for r in recs),
        "no_kill_after_finish": sum(r["outcome"] == "no_kill_after_finish" for r in recs),
        "killed_and_succeeded": sum(r["status"] == "succeeded" for r in killed),
        "resume_to_completion_rate": (
            round(sum(r["status"] == "succeeded" for r in killed) / len(killed), 4)
            if killed
            else None
        ),
        "all_runs_succeeded": sum(r["status"] == "succeeded" for r in recs),
        "runs_failing_check_run": sum(bool(r["check_problems"]) for r in recs),
        "duplicate_model_calls": sum(
            any("repeats" in p for p in r["check_problems"]) for r in recs
        ),
        "domains_with_more_than_one_company": len(dup_companies),
        "write_invocations_executed_more_than_once": len(dup_exec),
        "runs_with_company_rows_not_1": sum(r["company_rows"] != 1 for r in recs),
        "pre_kill_rows_replayed": sum(r["pre_kill_rows"] for r in killed),
        "pre_kill_model_calls_not_repaid": sum(r["pre_kill_model_calls"] for r in killed),
        "tool_calls_re_executed_after_crash": sum(r["rerun_tool_calls"] for r in killed),
        "save_company_reissued_but_executed_once": sum(
            r["save_attempts"] > 1 and r["save_executions_max"] == 1 for r in recs
        ),
        "kills_after_final_before_finish": sum(
            r.get("pre_kill_last_kind") == "final" for r in killed
        ),
        "tokens_a_cold_restart_would_respend": pre_tokens,
        "tokens_total_in_killed_runs": total_tokens_killed,
        "token_share_saved_vs_cold_restart": (
            round(pre_tokens / total_tokens_killed, 4) if total_tokens_killed else None
        ),
        "median_time_to_resume_s": round(statistics.median(resumed), 2) if resumed else None,
        "p90_time_to_resume_s": (
            round(statistics.quantiles(resumed, n=10)[-1], 2) if len(resumed) >= 10 else None
        ),
    }


# ── main ─────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200, help="total iterations wanted")
    ap.add_argument("--summary", action="store_true", help="only (re)write results.json")
    args = ap.parse_args()

    if not args.summary:
        if not settings.relay_api_key:
            sys.exit("RELAY_API_KEY is not set in .env")
        done = (
            len(ITERATIONS.read_text(encoding="utf-8").splitlines()) if ITERATIONS.exists() else 0
        )
        print(
            f"chaos: {done}/{args.n} done; lease={settings.lease_seconds}s "
            f"delay={settings.fake_delay_seconds}s",
            flush=True,
        )
        ensure_workers()
        for i in range(done, args.n):
            rec = iteration(i)
            with ITERATIONS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
            flag = "" if not rec["check_problems"] and rec["company_rows"] == 1 else "  <-- CHECK"
            print(
                f"[{i + 1:>3}/{args.n}] {rec['outcome']:<22} {rec['status']:<9} "
                f"attempts={rec['attempts']} rows={rec['rows']} "
                f"resume={rec.get('resume_s')} ({rec['elapsed_s']}s){flag}",
                flush=True,
            )

    if ITERATIONS.exists():
        results = summarize()
        RESULTS.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps(results, indent=2))
    pool.close()


if __name__ == "__main__":
    main()
