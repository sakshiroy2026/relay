"""P1/P3/P7 proof: a few REAL runs on the real model, with a hard spending cap.

WHAT IT IS  The first real check. Creates N runs for a 'live' tenant (real model,
            real tools), one at a time, with one local worker, and records what
            each one did and cost. Used three times: P1 (does it work at all?),
            P3 (LLM_EFFORT=low vs medium, is the prompt cache hit?) and P7 (the
            same on Gemini's free tier, where the costs below are what the run
            WOULD have cost on the paid tier; the invoice is $0).
GOES IN     python -m scripts.live_smoke --yes-spend [--runs 3] [--max-total-usd 0.10]
                [--domains razorpay.com,stripe.com]
            Needs the live provider's key in the environment (GEMINI_API_KEY,
            or ANTHROPIC_API_KEY while LLM_PROVIDER names Anthropic), plus
            TAVILY_API_KEY for real search, and a database at migration 008+.
            Refuses to start without --yes-spend and without that key.
COMES OUT   The estimate BEFORE anything is spent, then one line per run, then
            chaos/live_smoke_results.json (statuses, cost, tokens, cache share,
            the records, check_run problems).
TOUCHES     tenants (a 'live-smoke' tenant, created once), runs/steps/companies,
            one local worker process, the live provider's API, Tavily, the web.
FAILS WHEN  Spend reaches --max-total-usd -> stops before the next run (each run
            is also capped by its own budget_usd; at most one call can
            overshoot it). No key -> refuses to start.
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from chaos.harness import query_all, query_one, spawn_workers, stop_workers, wait_terminal
from relay.config import settings
from relay.db import pool
from relay.llm.factory import planner_model, resolve_mode
from relay.llm.pricing import estimate
from scripts.check_run import check_run

RESULTS = Path(__file__).parent.parent / "chaos" / "live_smoke_results.json"
# ~40k input + 4k output per run (CLAUDE.md). Priced from the model the live
# mode actually uses, so the estimate follows the provider instead of a comment.
EST_TOKENS = (40_000, 4_000)
# Which key each provider needs, and whether using it costs money.
PROVIDER_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
FREE_TIER_PROVIDERS = ("gemini",)


def live_tenant() -> uuid.UUID:
    with pool.connection() as conn:
        row = conn.execute("SELECT id FROM tenants WHERE name = 'live-smoke'").fetchone()
        if row is None:
            row = conn.execute(
                "INSERT INTO tenants (name, run_mode, max_concurrent_runs) "
                "VALUES ('live-smoke', 'live', 2) RETURNING id"
            ).fetchone()
    assert row is not None
    return row[0]


def create_live_run(tenant_id: uuid.UUID, domain: str) -> str:
    payload = {"domain": domain, "mode": "live"}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (tenant_id, input, agent_version, budget_usd) "
                "VALUES (%s, %s, 'enrich@v2', %s) RETURNING id",
                (tenant_id, Jsonb(payload), settings.live_run_budget_usd),
            )
            row = cur.fetchone()
            assert row is not None
            cur.execute(
                "INSERT INTO steps (run_id, step_index, kind, payload, written_by) "
                "VALUES (%s, 0, 'run_started', %s, 'live_smoke')",
                (row[0], Jsonb(payload)),
            )
    return str(row[0])


def summarize_run(run_id: str) -> dict[str, Any]:
    run = query_one(
        "SELECT status::text AS status, result, spent_usd, tokens_in, tokens_out "
        "FROM runs WHERE id = %s",
        (run_id,),
    )
    calls = query_all(
        "SELECT payload FROM steps WHERE run_id = %s AND kind = 'model_call' ORDER BY step_index",
        (run_id,),
    )
    errors = query_all("SELECT payload FROM steps WHERE run_id = %s AND kind = 'error'", (run_id,))
    uncached = sum(c["payload"].get("input_tokens", 0) for c in calls)
    cached = sum(c["payload"].get("cache_read_tokens", 0) for c in calls)
    return {
        "run_id": run_id[:8],
        "status": run["status"],
        "cost_usd": float(run["spent_usd"]),
        "model_calls": len(calls),
        "tokens_in_uncached": uncached,
        "tokens_cache_read": cached,
        "cache_read_share": round(cached / (cached + uncached), 3) if cached + uncached else 0,
        "tokens_out": int(run["tokens_out"]),
        "errors": [e["payload"].get("reason") for e in errors],
        "record": run["result"],
        "ledger_problems": check_run(run_id),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes-spend", action="store_true", help="required: this costs money")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--max-total-usd", type=float, default=0.10)
    parser.add_argument("--domains", default="razorpay.com,stripe.com,anthropic.com")
    args = parser.parse_args()

    domains = [d.strip() for d in args.domains.split(",") if d.strip()]
    provider, _tools = resolve_mode("live")
    model = planner_model(provider)
    est = args.runs * estimate(model, *EST_TOKENS)
    free = provider in FREE_TIER_PROVIDERS
    print(
        f"provider {provider}, model {model}, {args.runs} runs over {domains}\n"
        f"priced ~${est:.3f}; hard stop at ${args.max_total_usd:.2f} total, "
        f"${settings.live_run_budget_usd:.2f} per run"
        + (
            "\nthis provider's FREE tier is in use: the real bill is $0 and the "
            "numbers above are what the paid tier would have charged"
            if free
            else ""
        )
    )
    key = PROVIDER_KEYS.get(provider, "")
    if key and not os.environ.get(key):
        sys.exit(f"refusing: {key} is not set")
    if not args.yes_spend:
        sys.exit("refusing: pass --yes-spend to confirm you accept this cost")

    tenant = live_tenant()
    workers = spawn_workers(1, {})
    spent = 0.0
    lines: list[dict[str, Any]] = []
    try:
        time.sleep(2)
        for i in range(args.runs):
            if spent >= args.max_total_usd:
                print(f"stopping: ${spent:.4f} spent, cap ${args.max_total_usd:.2f}")
                break
            run_id = create_live_run(tenant, domains[i % len(domains)])
            wait_terminal([run_id], timeout_s=300)
            line = summarize_run(run_id)
            spent += line["cost_usd"]
            lines.append(line)
            print(
                f"{line['run_id']} {line['status']:<9} ${line['cost_usd']:.5f} "
                f"{line['model_calls']} calls, cache {line['cache_read_share']:.0%}, "
                f"errors {line['errors']}  (total ${spent:.4f})",
                flush=True,
            )
    finally:
        stop_workers(workers)
        pool.close()

    summary = {
        "conditions": {
            "provider": provider,
            "free_tier": free,
            "model": model,
            "effort": settings.llm_effort,
            "prompt_cache": settings.llm_prompt_cache,
            "tools_provider": "real",
            "per_run_budget_usd": settings.live_run_budget_usd,
        },
        "runs": lines,
        "total_cost_usd": round(spent, 6),
        "succeeded": sum(1 for x in lines if x["status"] == "succeeded"),
    }
    RESULTS.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"wrote {RESULTS}")


if __name__ == "__main__":
    main()
