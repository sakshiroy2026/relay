"""Fencing under stalls (blueprint §13, Test A3): freeze the owner, let another worker
take over, then wake the frozen one up and watch it get stopped.

WHAT IT IS  The scripted version of the Day 3 `docker pause` test. A frozen
            worker doesn't know it is frozen: when it wakes up it carries on
            from the exact line it was on, with a lease that has long passed.
            This harness does that N times at random moments and records which
            guard stopped the zombie: the heartbeat at the top of a turn, the
            UNIQUE(run_id, step_index) constraint on its next write, or the
            guarded finish. For each iteration:
              1. create a run; wait for a worker to claim it
              2. wait a random 0..(one run's duration), then SIGSTOP the owner
                 (what `docker pause` does to a container)
              3. wait until another worker holds the lease
              4. wait a random 0-2 s more (so some zombies wake mid-takeover and
                 some after the run already finished), then SIGCONT
              5. wait for the run to finish and for the zombie to react
GOES IN     python -m chaos.fencing_test [--n 100] [--workers 3] [--lease 3]
                [--delay 0.3] [--note "..."]
            Needs a database with migrations 001-003. Workers are local
            `python -m relay.worker` processes (no Docker); runs are created
            with the API's two inserts.
COMES OUT   chaos/fencing_iterations.jsonl (one line per iteration) and
            chaos/fencing_results.json (the summary, conditions next to the
            numbers).
TOUCHES     runs/steps/companies/tool_invocations; local processes (signals);
            worker logs in a fresh temp directory.
FAILS WHEN  The database is unreachable. A zombie that finishes a run it no
            longer owns, a ledger that check_run rejects, or a run that does
            not end `succeeded` -> counted in the summary, exit code 1.
"""

import argparse
import json
import random
import re
import signal
import statistics
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from chaos.harness import (
    Worker,
    create_runs,
    db_now,
    percentile,
    query_one,
    spawn_workers,
    stop_workers,
    wait_terminal,
)
from relay.core.replay import rebuild_messages
from relay.db import pool
from scripts.check_run import find_problems, load_steps

HERE = Path(__file__).parent
ITERATIONS = HERE / "fencing_iterations.jsonl"
RESULTS = HERE / "fencing_results.json"
RESUME_JITTER_S = 2.0
ZOMBIE_REACTION_TIMEOUT_S = 15.0


# ── reading the run ──────────────────────────────────────────────────────
def run_row(run_id: str) -> dict[str, Any]:
    return query_one(
        "SELECT status::text AS status, lease_owner, attempt_count, started_at, finished_at "
        "FROM runs WHERE id = %s",
        (run_id,),
    )


def wait_for(cond: Any, timeout_s: float, poll_s: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(poll_s)
    return False


def next_kind(rows: Sequence[Mapping[str, Any]]) -> str:
    """What a worker that had written/read exactly `rows` would write next.

    A fenced zombie's log says only "step N ... already written". Rows 0..N-1
    are what it knew when it tried N (ledger rows never change), so its next
    move can be worked out with the same replay the next worker uses.
    """
    if rows[-1]["kind"] == "tool_call":
        return "tool_result"
    state = rebuild_messages(rows)
    if state.final is not None or state.failed:
        return "nothing (already decided)"
    if state.unjudged_answer is not None:
        return "final"
    if state.pending_tool_calls:
        return "tool_call"
    return "model_call"


ZOMBIE_LINE = re.compile(r"\] (lease_lost|LOST|finished|failed) (\S+)(.*)")


def zombie_reaction(text: str, run_id: str) -> tuple[str, str] | None:
    """The zombie's first log line about this run after it woke: (event, detail)."""
    for m in ZOMBIE_LINE.finditer(text):
        if run_id.startswith(m.group(2).rstrip(":")) or m.group(2) == run_id:
            return m.group(1), m.group(3).strip()
    return None


# ── one iteration ────────────────────────────────────────────────────────
def iteration(i: int, workers: list[Worker], span_s: float, rng: random.Random) -> dict[str, Any]:
    by_id = {w.worker_id: w for w in workers}
    log_marks = {w.worker_id: len(w.log_text()) for w in workers}
    domain = f"fence-{i}-{uuid.uuid4().hex[:6]}.example"
    (run_id,) = create_runs([domain], written_by="fencing_test")
    out: dict[str, Any] = {"i": i, "run_id": run_id}

    if not wait_for(lambda: run_row(run_id)["lease_owner"] is not None, 30):
        return {**out, "outcome": "never claimed"}
    zombie_id = run_row(run_id)["lease_owner"]
    zombie = by_id[zombie_id]

    time.sleep(rng.uniform(0, span_s))
    zombie.proc.send_signal(signal.SIGSTOP)
    paused_at = db_now()
    before = load_steps(run_id)  # what was committed when it froze
    if run_row(run_id)["status"] != "running":
        zombie.proc.send_signal(signal.SIGCONT)
        return {**out, "outcome": "no pause (run finished first)"}
    known = {s["step_index"] for s in before}
    out["paused_after"] = f"step {before[-1]['step_index']} ({before[-1]['kind']})"

    took_over = wait_for(lambda: run_row(run_id)["lease_owner"] != zombie_id, 60, poll_s=0.02)
    takeover_s = (db_now() - paused_at).total_seconds()
    if not took_over:
        zombie.proc.send_signal(signal.SIGCONT)
        return {**out, "outcome": "no takeover within 60 s"}

    time.sleep(rng.uniform(0, RESUME_JITTER_S))
    status_at_wake = run_row(run_id)["status"]
    wake_mark = len(zombie.log_text())
    zombie.proc.send_signal(signal.SIGCONT)

    statuses = wait_terminal([run_id], timeout_s=90, poll_s=0.05)
    reacted = wait_for(
        lambda: zombie_reaction(zombie.log_text()[wake_mark:], run_id) is not None,
        ZOMBIE_REACTION_TIMEOUT_S,
        poll_s=0.05,
    )
    reaction = zombie_reaction(zombie.log_text()[wake_mark:], run_id) if reacted else None

    steps = load_steps(run_id)
    stale = [s for s in steps if s["written_by"] == zombie_id and s["step_index"] not in known]
    stopped_by, rejected_kind = "no reaction", None
    if reaction is not None:
        event, detail = reaction
        if event == "lease_lost" and "before turn" in detail:
            stopped_by = "heartbeat (lease gone at the top of a turn)"
        elif event == "lease_lost" and "already written" in detail:
            stopped_by = "UNIQUE(run_id, step_index) on its next write"
            n = int(re.search(r"step (\d+)", detail).group(1))  # type: ignore[union-attr]
            rejected_kind = next_kind(steps[:n])
        elif event == "LOST":
            stopped_by = "guarded finish (lease_owner = me AND status = 'running')"
        else:
            stopped_by = f"ZOMBIE {event.upper()} THE RUN"

    # other workers fenced by the zombie's stale write (it landed first), and what they lost
    others_lost: list[str] = []
    for wid, w in by_id.items():
        if wid == zombie_id:
            continue
        for m in re.finditer(
            rf"lease_lost {run_id[:8]}: step (\d+) of", w.log_text()[log_marks[wid] :]
        ):
            others_lost.append(next_kind(steps[: int(m.group(1))]))
    final = run_row(run_id)
    executions = query_one(
        "SELECT COALESCE(max(executions), 0) AS m FROM tool_invocations WHERE run_id = %s",
        (run_id,),
    )["m"]
    companies = query_one("SELECT count(*) AS n FROM companies WHERE domain = %s", (domain,))["n"]
    return {
        **out,
        "outcome": "paused",
        "status": final["status"],
        "statuses": statuses,
        "attempts": final["attempt_count"],
        "takeover_s": round(takeover_s, 2),
        "run_status_when_zombie_woke": status_at_wake,
        "stopped_by": stopped_by,
        "rejected_write_kind": rejected_kind,
        "stale_writes_accepted": len(stale),
        "stale_write_kinds": [s["kind"] for s in stale],
        "other_workers_fenced": len(others_lost),
        "other_workers_lost_write_kinds": others_lost,
        # the stale execution ending in finished/failed would be the real bug; the zombie
        # legitimately claiming the run again later (a fresh attempt) is fine
        "zombie_finished_while_stale": stopped_by.startswith("ZOMBIE"),
        "zombie_reclaimed_it_later": f"claimed {run_id}" in zombie.log_text()[wake_mark:],
        "ledger_problems": find_problems(steps),
        "max_executions_per_key": executions,
        "company_rows": companies,
    }


# ── summary ──────────────────────────────────────────────────────────────
def summarize(lines: list[dict[str, Any]], conditions: dict[str, Any]) -> dict[str, Any]:
    paused = [x for x in lines if x["outcome"] == "paused"]
    takeovers = [x["takeover_s"] for x in paused]
    rejected = Counter(x["rejected_write_kind"] for x in paused if x["rejected_write_kind"])
    return {
        "conditions": conditions,
        "iterations": len(lines),
        "outcomes": dict(Counter(x["outcome"] for x in lines)),
        "paused_runs_that_ended_succeeded": sum(1 for x in paused if x["status"] == "succeeded"),
        "how_the_zombie_was_stopped": dict(Counter(x["stopped_by"] for x in paused)),
        "write_the_zombie_lost_to_the_constraint": dict(rejected),
        "model_calls_paid_for_then_thrown_away_by_fencing": rejected.get("model_call", 0)
        + sum(x["other_workers_lost_write_kinds"].count("model_call") for x in paused),
        "zombie_woke_while_run_still_running": sum(
            1 for x in paused if x["run_status_when_zombie_woke"] == "running"
        ),
        "stale_writes_accepted_total": sum(x["stale_writes_accepted"] for x in paused),
        "runs_with_a_stale_write_accepted": sum(1 for x in paused if x["stale_writes_accepted"]),
        "stale_write_kinds": dict(Counter(k for x in paused for k in x["stale_write_kinds"])),
        "new_owners_fenced_by_a_zombie_write_that_landed_first": sum(
            x["other_workers_fenced"] for x in paused
        ),
        "runs_finished_by_a_stale_zombie": sum(
            1 for x in paused if x["zombie_finished_while_stale"]
        ),
        "runs_the_woken_zombie_later_reclaimed_legitimately": sum(
            1 for x in paused if x["zombie_reclaimed_it_later"]
        ),
        "runs_with_ledger_problems": sum(1 for x in paused if x["ledger_problems"]),
        "keys_executed_more_than_once": sum(1 for x in paused if x["max_executions_per_key"] > 1),
        "domains_with_company_rows_not_exactly_1": sum(1 for x in paused if x["company_rows"] != 1),
        "takeover_s": {
            "median": round(statistics.median(takeovers), 2) if takeovers else None,
            "p90": round(percentile(takeovers, 90), 2) if takeovers else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--lease", type=int, default=3)
    parser.add_argument("--delay", type=float, default=0.3)
    parser.add_argument("--seed", type=int, default=16)
    parser.add_argument("--note", default="", help="recorded next to the numbers, e.g. which DB")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    env = {"LEASE_SECONDS": str(args.lease), "FAKE_DELAY_SECONDS": str(args.delay)}
    log_dir = Path(tempfile.mkdtemp(prefix="relay-fencing-"))
    workers = spawn_workers(args.workers, env, log_dir)
    started = time.monotonic()
    lines: list[dict[str, Any]] = []
    try:
        time.sleep(2)  # imports + pool
        (warm,) = create_runs([f"fence-warm-{uuid.uuid4().hex[:6]}.example"], "fencing_test")
        wait_terminal([warm], timeout_s=60)
        w = run_row(warm)
        span_s = (w["finished_at"] - w["started_at"]).total_seconds()
        print(f"one run takes {span_s:.2f} s; pausing at a random moment inside that", flush=True)

        ITERATIONS.write_text("", encoding="utf-8")
        for i in range(args.n):
            line = iteration(i, workers, span_s, rng)
            lines.append(line)
            with ITERATIONS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, default=str) + "\n")
            print(
                f"{i:3d} {line['run_id'][:8]} {line['outcome']:<8} "
                f"{line.get('stopped_by', '')} {line.get('rejected_write_kind') or ''} "
                f"stale={line.get('stale_writes_accepted', '-')} {line.get('status', '')}",
                flush=True,
            )
    finally:
        for wk in workers:  # never leave a frozen process behind
            if wk.proc.poll() is None:
                wk.proc.send_signal(signal.SIGCONT)
        stop_workers(workers)

    conditions = {
        "workers": args.workers,
        "lease_seconds": args.lease,
        "fake_delay_seconds": args.delay,
        "one_run_seconds": round(span_s, 2),
        "pause": "SIGSTOP of the run's owner at a uniform random moment within one run's duration",
        "wake": f"SIGCONT 0-{RESUME_JITTER_S} s (uniform) after another worker took the lease",
    }
    summary = summarize(lines, conditions)
    summary["elapsed_s"] = round(time.monotonic() - started, 1)
    if args.note:
        summary["environment_note"] = args.note
    RESULTS.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str))
    pool.close()

    paused = [x for x in lines if x["outcome"] == "paused"]
    bad = (
        summary["runs_finished_by_a_stale_zombie"]
        + summary["runs_with_ledger_problems"]
        + summary["keys_executed_more_than_once"]
        + summary["domains_with_company_rows_not_exactly_1"]
        + sum(1 for x in paused if x["status"] != "succeeded")
    )
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
