"""Day 9 proofs: the repair ladder's success rate, and the DLQ round trip.

WHAT IT IS  Three scenarios, numbers instead of trust.
            `repair`    — N runs per breakage kind (clean, fenced, prose,
                          bad_year, hopeless; see relay/agent/fake_script.py),
                          measuring which rung saved the run and what rung 2
                          cost in extra model calls.
            `outage`    — the model is fully down: every run fails with a
                          `model_unavailable` error step. The DLQ requeues them
                          all, healthy workers drain them, and the ledgers must
                          show the runs RESUMED (rows written before the
                          failure kept, no model call paid twice) rather than
                          restarted.
            `exhausted` — each run's owner is killed on every claim until
                          attempt_count reaches max_attempts; the reaper must
                          mark the run dead, and a requeue (which raises
                          max_attempts) must let it finish.
GOES IN     python -m chaos.repair_dlq_test [--n 20] [--workers 3]
                [--exhausted-n 5] [--note "..."]
            Needs a database with migrations 001-004. Workers are local
            processes (no Docker, no API), like the other Day 16 harnesses.
COMES OUT   chaos/repair_dlq_results.json, conditions next to the numbers.
TOUCHES     runs/steps/companies/tool_invocations; local processes.
FAILS WHEN  The database is unreachable. A run that should be repaired and
            isn't, a requeued run that restarted instead of resuming, a ledger
            problem, or a repair on a clean run -> counted, exit code 1.
"""

import argparse
import json
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from chaos.harness import (
    create_runs,
    query_all,
    query_one,
    spawn_workers,
    stop_workers,
    wait_terminal,
)
from relay.core.dlq import requeue_run
from relay.db import pool
from scripts.check_run import check_run

HERE = Path(__file__).parent
RESULTS = HERE / "repair_dlq_results.json"
BREAKAGES = ("clean", "fenced", "prose", "bad_year", "hopeless")


def ledger_problems(run_ids: list[str]) -> dict[str, list[str]]:
    problems = {rid: check_run(rid) for rid in run_ids}
    return {k: v for k, v in problems.items() if v}


def rows_of(run_ids: list[str]) -> list[dict[str, Any]]:
    return query_all(
        "SELECT run_id, step_index, kind::text AS kind, payload, written_by FROM steps "
        "WHERE run_id = ANY(%s) ORDER BY run_id, step_index",
        ([uuid.UUID(r) for r in run_ids],),
    )


# ── scenario: repair ─────────────────────────────────────────────────────
def scenario_repair(n: int, workers: int) -> dict[str, Any]:
    out: dict[str, Any] = {"conditions": {"runs_per_kind": n, "workers": workers}}
    strict_would_have_failed = saved = 0
    for kind in BREAKAGES:
        procs = spawn_workers(workers, {} if kind == "clean" else {"FAKE_BROKEN_FINAL": kind})
        try:
            time.sleep(1.5)
            run_ids = create_runs(
                [f"repair-{kind}-{i}-{uuid.uuid4().hex[:4]}.example" for i in range(n)],
                written_by="repair_dlq_test",
            )
            statuses = wait_terminal(run_ids, timeout_s=180)
        finally:
            stop_workers(procs)

        rows = rows_of(run_ids)
        finals = [r["payload"] for r in rows if r["kind"] == "final"]
        repaired: Counter[str] = Counter()
        for p in finals:
            for tag in p.get("repaired_by", []):
                repaired[tag] += 1
        reasks = sum(1 for r in rows if r["kind"] == "repair_requested")
        reask_cost = sum(
            float(r["payload"]["cost_usd"])
            for r in rows
            if r["kind"] == "model_call" and r["payload"]["stop_reason"] == "end_turn"
        ) / max(1, len(run_ids))  # avg cost of final-shaped calls, for scale
        out[kind] = {
            "statuses": statuses,
            "finals_accepted_as_is": sum(1 for p in finals if "repaired_by" not in p),
            "repaired_by": dict(repaired),
            "reask_model_calls_paid": reasks,
            "avg_end_turn_call_cost_usd": round(reask_cost, 6),
            "invalid_final_answer_errors": sum(
                1
                for r in rows
                if r["kind"] == "error" and r["payload"].get("reason") == "invalid_final_answer"
            ),
            "ledger_problems": ledger_problems(run_ids),
        }
        if kind != "clean":  # before Day 9, every one of these runs failed on the spot
            strict_would_have_failed += n
            saved += statuses.get("succeeded", 0) if kind != "hopeless" else 0
    out["summary"] = {
        "runs_a_strict_parser_would_have_failed": strict_would_have_failed,
        "saved_by_the_ladder": saved,
        "repair_success_rate_on_repairable_breakage": f"{saved}/{3 * n}",
    }
    return out


# ── scenario: outage -> DLQ -> requeue ───────────────────────────────────
def scenario_outage(n: int, workers: int) -> dict[str, Any]:
    # 0.9 per call (retries make that ~0.73 per dispatch): runs die at a RANDOM
    # turn, so most have already paid for model calls the requeue must not re-pay.
    procs = spawn_workers(workers, {"FAKE_MODEL_FAILURE_RATE": "0.9"})
    try:
        time.sleep(1.5)
        run_ids = create_runs(
            [f"dlq-outage-{i}-{uuid.uuid4().hex[:4]}.example" for i in range(n)],
            written_by="repair_dlq_test",
        )
        failed_statuses = wait_terminal(run_ids, timeout_s=180)
    finally:
        stop_workers(procs)

    rows_before = rows_of(run_ids)
    indexes_before = {
        rid: max(r["step_index"] for r in rows_before if str(r["run_id"]) == rid) for rid in run_ids
    }
    reasons = Counter(r["payload"].get("reason") for r in rows_before if r["kind"] == "error")

    failed_ids = [
        str(r["id"])
        for r in query_all(
            "SELECT id FROM runs WHERE id = ANY(%s) AND status = 'failed'",
            ([uuid.UUID(r) for r in run_ids],),
        )
    ]
    requeue_outcomes = Counter(
        requeue_run(uuid.UUID(rid), requeued_by="repair_dlq_test") for rid in failed_ids
    )

    procs = spawn_workers(workers, {})  # the outage is over
    try:
        drained_statuses = wait_terminal(run_ids, timeout_s=180)
    finally:
        stop_workers(procs)

    rows_after = rows_of(run_ids)
    resumed = restarted = 0
    model_calls_before_kept = 0
    for rid in failed_ids:
        mine = [r for r in rows_after if str(r["run_id"]) == rid]
        # resumed = every pre-failure row still there, new rows appended after the marker
        kinds = [r["kind"] for r in mine]
        if kinds.count("run_started") == 1 and "requeued" in kinds:
            first_new = next(i for i, r in enumerate(mine) if r["kind"] == "requeued")
            if mine[first_new]["step_index"] == indexes_before[rid] + 1:
                resumed += 1
            else:
                restarted += 1
        model_calls_before_kept += sum(
            1 for r in mine if r["kind"] == "model_call" and r["step_index"] <= indexes_before[rid]
        )
    return {
        "conditions": {"runs": n, "workers": workers, "fake_model_failure_rate_phase1": 0.9},
        "phase1_statuses": dict(failed_statuses),
        "phase1_error_reasons": dict(reasons),
        "requeue_outcomes": dict(requeue_outcomes),
        "phase2_statuses": dict(drained_statuses),
        "runs_resumed_from_their_ledger": resumed,
        "runs_restarted_from_scratch": restarted,
        "model_calls_made_before_the_outage_and_kept": model_calls_before_kept,
        "ledger_problems": ledger_problems(run_ids),
    }


# ── scenario: attempts exhausted -> reaper -> requeue ────────────────────
def scenario_exhausted(n: int, lease: int) -> dict[str, Any]:
    """One worker at a time; it is SIGKILLed every time it claims the run."""
    env = {"LEASE_SECONDS": str(lease), "FAKE_DELAY_SECONDS": "1.0"}
    results: list[dict[str, Any]] = []
    for i in range(n):
        (run_id,) = create_runs(
            [f"dlq-exhausted-{i}-{uuid.uuid4().hex[:4]}.example"], written_by="repair_dlq_test"
        )
        kills = 0
        while True:
            run = query_one(
                "SELECT status::text AS status, attempt_count, max_attempts, lease_owner "
                "FROM runs WHERE id = %s",
                (run_id,),
            )
            if run["status"] not in ("pending", "running"):
                break
            if run["attempt_count"] >= run["max_attempts"]:
                # out of attempts; one more (idle) worker must now reap it
                procs = spawn_workers(1, env)
                try:
                    deadline = time.monotonic() + lease + 30
                    while time.monotonic() < deadline:
                        if (
                            query_one(
                                "SELECT status::text AS s FROM runs WHERE id = %s", (run_id,)
                            )["s"]
                            == "dead"
                        ):
                            break
                        time.sleep(0.3)
                finally:
                    stop_workers(procs)
                break
            procs = spawn_workers(1, env)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    owner = query_one(
                        "SELECT lease_owner, attempt_count FROM runs WHERE id = %s", (run_id,)
                    )
                    if owner["attempt_count"] > kills:  # it claimed: kill it mid-run
                        time.sleep(0.5)
                        procs[0].proc.kill()  # SIGKILL on POSIX, TerminateProcess on Windows
                        kills += 1
                        break
                    time.sleep(0.1)
            finally:
                stop_workers(procs)
            time.sleep(lease + 0.5)  # let the lease expire before the next claim

        dead_row = query_one(
            "SELECT status::text AS status, attempt_count, error FROM runs WHERE id = %s",
            (run_id,),
        )
        outcome = requeue_run(uuid.UUID(run_id), requeued_by="repair_dlq_test")
        procs = spawn_workers(1, env)
        try:
            final_statuses = wait_terminal([run_id], timeout_s=120)
        finally:
            stop_workers(procs)
        results.append(
            {
                "run_id": run_id[:8],
                "kills": kills,
                "dead": dead_row["status"] == "dead",
                "reason": (dead_row["error"] or {}).get("reason"),
                "requeue": outcome,
                "final": dict(final_statuses),
                "problems": check_run(run_id),
            }
        )
        print(f"  exhausted {i}: {results[-1]}", flush=True)

    return {
        "conditions": {"runs": n, "lease_seconds": lease, "max_attempts": 5},
        "runs_dead_lettered_by_the_reaper": sum(1 for r in results if r["dead"]),
        "dead_reasons": dict(Counter(r["reason"] for r in results)),
        "requeue_outcomes": dict(Counter(r["requeue"] for r in results)),
        "requeued_runs_that_then_succeeded": sum(
            1 for r in results if r["final"].get("succeeded") == 1
        ),
        "runs_with_ledger_problems": sum(1 for r in results if r["problems"]),
        "per_run": results,
    }


# ── main ─────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--exhausted-n", type=int, default=5)
    parser.add_argument("--lease", type=int, default=3)
    parser.add_argument("--note", default="", help="recorded next to the numbers, e.g. which DB")
    args = parser.parse_args()

    started = time.monotonic()
    summary: dict[str, Any] = {}
    print("scenario repair ...", flush=True)
    summary["repair"] = scenario_repair(args.n, args.workers)
    print("scenario outage ...", flush=True)
    summary["outage"] = scenario_outage(args.n, args.workers)
    print("scenario exhausted ...", flush=True)
    summary["exhausted"] = scenario_exhausted(args.exhausted_n, args.lease)
    summary["elapsed_s"] = round(time.monotonic() - started, 1)
    if args.note:
        summary["environment_note"] = args.note

    RESULTS.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str))
    pool.close()

    rep = summary["repair"]
    bad = (
        sum(len(rep[k]["ledger_problems"]) for k in BREAKAGES)
        + sum(rep[k]["statuses"].get("failed", 0) for k in ("clean", "fenced", "prose", "bad_year"))
        + (0 if rep["clean"]["repaired_by"] == {} else 1)  # a clean run must never be "repaired"
        + summary["outage"]["runs_restarted_from_scratch"]
        + len(summary["outage"]["ledger_problems"])
        + (args.n - summary["outage"]["phase2_statuses"].get("succeeded", 0))  # requeued + lucky
        + (args.exhausted_n - summary["exhausted"]["requeued_runs_that_then_succeeded"])
        + summary["exhausted"]["runs_with_ledger_problems"]
    )
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
