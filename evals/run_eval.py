"""Run the golden set through Relay and score it (P6, Day 19/20).

WHAT IT IS  The eval loop: one run per hand-labelled company, scored field by
            field (evals/scoring.py), industry judged by an LLM (evals/judge.py).
            Two modes:
              demo — the scripted fake model, $0: proves the plumbing and the
                     scorer end to end; results go to results.demo.json and can
                     never satisfy the CI gate.
              live — the real model + real tools; costs money, so it refuses
                     without --yes-spend, prints the estimate first, and stops
                     creating runs at --max-total-usd.
GOES IN     python -m evals.run_eval --golden evals/golden.jsonl --mode live --yes-spend
                [--judge-model gemini-3.8-flash | --no-judge] [--workers 2]
                [--max-total-usd 1.0]
            Labels: one JSON object per line, {"domain", "expected": {field: value
            or null}, "notes"?} (format: evals/README.md).
COMES OUT   evals/results.json (live) or evals/results.demo.json (demo): every
            field outcome, the summary, cost (runs + judge), model, git commit.
TOUCHES     tenants (an 'eval-live' / 'eval-demo' tenant), runs/steps/companies,
            local worker processes; in live mode the Gemini API (runs and the
            judge, GEMINI_API_KEY) and the web (Tavily, TAVILY_API_KEY).
FAILS WHEN  No labels -> exits 2. A run that never finishes is scored as a
            missing record (every field wrong), not dropped.
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

from psycopg.types.json import Jsonb

from chaos.harness import query_one, spawn_workers, stop_workers, wait_terminal
from evals.judge import DEFAULT_MODEL as JUDGE_MODEL
from evals.judge import IndustryJudge
from evals.scoring import score_record, summarize
from relay.config import settings
from relay.db import pool
from relay.llm.factory import planner_model, resolve_mode

HERE = Path(__file__).parent
# Priced at Google's PAID rates (the free tier bills $0): a measured Flash-Lite
# run was $0.003 (b0a3aea7); one short 3.8-Flash comparison is ~$0.001. The
# results record the real figures.
EST_RUN_USD = 0.005
EST_JUDGE_USD = 0.001


def load_labels(path: Path) -> list[dict[str, Any]]:
    labels = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        row = json.loads(line)
        if "domain" not in row or not isinstance(row.get("expected"), dict):
            sys.exit(f"{path}:{n}: each line needs 'domain' and an 'expected' object")
        labels.append(row)
    return labels


def tenant(mode: str) -> uuid.UUID:
    name = f"eval-{mode}"
    with pool.connection() as conn:
        row = conn.execute("SELECT id FROM tenants WHERE name = %s", (name,)).fetchone()
        if row is None:
            row = conn.execute(
                "INSERT INTO tenants (name, run_mode) VALUES (%s, %s) RETURNING id", (name, mode)
            ).fetchone()
    assert row is not None
    return row[0]


def create_run(tenant_id: uuid.UUID, domain: str, mode: str) -> str:
    payload = {"domain": domain, "mode": mode}
    budget = settings.live_run_budget_usd if mode == "live" else 0.25
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (tenant_id, input, agent_version, budget_usd) "
                "VALUES (%s, %s, 'enrich@v2', %s) RETURNING id",
                (tenant_id, Jsonb(payload), budget),
            )
            row = cur.fetchone()
            assert row is not None
            cur.execute(
                "INSERT INTO steps (run_id, step_index, kind, payload, written_by) "
                "VALUES (%s, 0, 'run_started', %s, 'run_eval')",
                (row[0], Jsonb(payload)),
            )
    return str(row[0])


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=HERE / "golden.jsonl")
    parser.add_argument("--mode", choices=["demo", "live"], default="demo")
    parser.add_argument("--yes-spend", action="store_true", help="required for live / the judge")
    parser.add_argument("--judge-model", default=JUDGE_MODEL)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-total-usd", type=float, default=1.0)
    args = parser.parse_args()

    labels = load_labels(args.golden)
    if not labels:
        sys.exit(2)
    use_judge = not args.no_judge and (args.mode == "live" or args.yes_spend)
    est = (EST_RUN_USD * len(labels) if args.mode == "live" else 0.0) + (
        EST_JUDGE_USD * len(labels) if use_judge else 0.0
    )
    print(
        f"{len(labels)} labelled companies, mode {args.mode}, "
        f"judge {args.judge_model if use_judge else 'off (word-overlap heuristic)'}\n"
        f"estimate ~${est:.3f}; runs stop being created at ${args.max_total_usd:.2f}"
    )
    if est > 0 and not args.yes_spend:
        sys.exit("refusing: this costs money; pass --yes-spend to confirm")
    if est > 0 and not os.environ.get("GEMINI_API_KEY"):
        sys.exit("refusing: GEMINI_API_KEY is not set (the live runs and the judge need it)")
    if args.mode == "live" and not settings.tavily_api_key:
        sys.exit("refusing: TAVILY_API_KEY is not set (live runs search the real web)")

    judge = None
    if use_judge:
        judge = IndustryJudge(args.judge_model)

    tenant_id = tenant(args.mode)
    workers = spawn_workers(args.workers, {})
    rows: list[dict[str, Any]] = []
    spent = 0.0
    started = time.monotonic()
    try:
        time.sleep(2)
        for label in labels:
            if spent >= args.max_total_usd:
                print(f"stopping: ${spent:.4f} spent")
                break
            run_id = create_run(tenant_id, label["domain"], args.mode)
            wait_terminal([run_id], timeout_s=300)
            run = query_one(
                "SELECT status::text AS status, result, spent_usd FROM runs WHERE id = %s",
                (run_id,),
            )
            spent += float(run["spent_usd"]) if args.mode == "live" else 0.0
            scored = score_record(label["expected"], run["result"], judge.same if judge else None)
            ok = sum(1 for f in scored.values() if f["outcome"] == "correct")
            print(
                f"{label['domain']:<28} {run['status']:<9} {ok}/{len(scored)} correct", flush=True
            )
            rows.append(
                {
                    "domain": label["domain"],
                    "run_id": run_id[:8],
                    "status": run["status"],
                    "cost_usd": float(run["spent_usd"]),
                    "fields": scored,
                }
            )
    finally:
        stop_workers(workers)
        pool.close()

    summary = summarize([r["fields"] for r in rows])
    out = {
        "mode": args.mode,
        # the model the mode actually uses, not a hardcoded provider's
        "model": planner_model(resolve_mode(args.mode)[0]),
        "judge": args.judge_model if judge else None,
        "commit": git_commit(),
        "golden": str(args.golden),
        "labels": len(labels),
        "cost_usd": {
            "runs": round(spent, 6),
            "judge": round(judge.cost_usd, 6) if judge else 0.0,
        },
        "elapsed_s": round(time.monotonic() - started, 1),
        "summary": summary,
        "rows": rows,
        "judge_verdicts": judge.reasons if judge else [],
        "judge_errors": judge.errors if judge else [],
    }
    path = HERE / ("results.json" if args.mode == "live" else "results.demo.json")
    path.write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
    o = summary["overall"]
    print(
        f"accuracy {o['accuracy']}, precision {o['precision']}, abstained {o['abstention_rate']} "
        f"-> {path}"
    )


if __name__ == "__main__":
    main()
