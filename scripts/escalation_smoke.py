"""P5-on-Gemini proof: real runs down the escalation and fallback paths, on purpose.

WHAT IT IS  A real model rarely fails on cue, so this forces the failure and lets
            everything after it be real. A wrapper around the real adapter breaks
            Flash-Lite's final answers (rung 3) or pretends Flash-Lite is down for
            one turn (fallback). The loop, the ledger and the model that rescues
            the run are all real. Three scenarios, in-process, fake tools:
              rung3    answer -> re-ask -> real 3.8 Flash
              killed   the same, worker dies just before 3.8 Flash, a new one resumes
              fallback Flash-Lite "down" on its 2nd turn -> real 3.8 Flash mid-turn
GOES IN     python -m scripts.escalation_smoke --yes-spend [--max-requests 30]
            GEMINI_API_KEY in the environment; DATABASE_URL on THIS machine.
COMES OUT   One line per scenario, then chaos/escalation_smoke_results.json: run
            ids, the models asked, real requests, how the run ended, check_run
            problems. Forced failures are named as forced there.
TOUCHES     runs/steps/companies under the dev tenant (parked 'dead', so no worker
            claims them), and the Gemini API: free tier, spaced GAP_SECONDS apart.
FAILS WHEN  No --yes-spend, no key, or a database that isn't local -> refuses.
            A request past --max-requests -> RequestCapReached, stops everything.
"""

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
from psycopg.types.json import Jsonb

from relay.agent.fake_script import _FINAL_RECORD
from relay.agent.loop import run_agent
from relay.config import settings
from relay.core.retry import RetryableError
from relay.db import pool
from relay.llm import factory
from relay.llm.client import Response
from relay.llm.gemini_client import GeminiLLMClient
from scripts.check_run import find_problems, load_steps

RESULTS = Path(__file__).parent.parent / "chaos" / "escalation_smoke_results.json"
BROKEN = json.dumps({**_FINAL_RECORD, "founded_year": 1599})  # valid JSON, invalid value
GAP_SECONDS = 7  # the free tier allows about ten requests a minute
LOCAL_HOSTS = ("127.0.0.1", "localhost")


class RequestCapReached(Exception):
    """The agreed number of real requests is used up. Never retried."""


class WorkerKilled(Exception):
    """Stands in for kill -9 just before the escalated call."""


class Forced:
    """The real adapter, plus exactly one forced failure. Records every model asked."""

    def __init__(self, real: GeminiLLMClient, scenario: str, cheap: str, strong: str) -> None:
        self.real, self.scenario, self.cheap, self.strong = real, scenario, cheap, strong
        self.models: list[str] = []
        self.cheap_answers = 0

    def call(self, model: str, system: str, messages: Any, tools: Any) -> Response:
        self.models.append(model)
        if self.scenario == "killed" and model == self.strong:
            self.scenario = "resumed"  # the next worker's call goes through
            raise WorkerKilled
        if self.scenario == "fallback" and model == self.cheap:
            self.cheap_answers += 1
            if 2 <= self.cheap_answers <= 4:  # the 2nd turn's three tries
                raise RetryableError("forced: primary down")
        response = self.real.call(model=model, system=system, messages=messages, tools=tools)
        breaks = self.scenario in ("rung3", "killed")
        if breaks and model == self.cheap and response.stop_reason == "end_turn":
            return replace(response, content=BROKEN)  # raw parts stay real and signed
        return response


def parked_run() -> tuple[uuid.UUID, str]:
    domain = "razorpay.com"
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO runs (tenant_id, input, agent_version, status) "
            "VALUES (%s, %s, 'escalation-smoke', 'dead') RETURNING id",
            (settings.dev_tenant_id, Jsonb({"domain": domain})),
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO steps (run_id, step_index, kind, payload, written_by) "
            "VALUES (%s, 0, 'run_started', %s, 'escalation-smoke')",
            (row[0], Jsonb({"domain": domain})),
        )
    return row[0], domain


def run_scenario(scenario: str, real: GeminiLLMClient) -> dict[str, Any]:
    cheap = factory.planner_model("gemini")
    strong = factory.escalation_model("gemini")
    client = Forced(real, scenario, cheap, strong)
    factory._clients["gemini"] = client
    run_id, domain = parked_run()
    budget = settings.live_run_budget_usd
    workers = 1
    try:
        result = run_agent(run_id, domain, "w-a", lambda: True, budget, mode="public_real")
    except WorkerKilled:
        workers = 2
        result = run_agent(run_id, domain, "w-b", lambda: True, budget, mode="public_real")

    steps = load_steps(str(run_id))
    calls = [s["payload"] for s in steps if s["kind"] == "model_call"]
    final = next((s["payload"] for s in steps if s["kind"] == "final"), None)
    error = next((s["payload"] for s in steps if s["kind"] == "error"), None)
    return {
        "scenario": scenario,
        "forced": {
            "rung3": "Flash-Lite's final answers replaced with an invalid record",
            "killed": "the same, and the worker dies just before the escalated call",
            "fallback": "Flash-Lite raises RetryableError on its 2nd turn, 3 tries",
        }[scenario],
        "run_id": str(run_id)[:8],
        "ok": result is not None,
        "workers": workers,
        "models_asked": client.models,
        "models_billed": [c["model"] for c in calls],
        "fallback_from": [c["fallback_from"] for c in calls if c.get("fallback_from")],
        "repaired_by": final.get("repaired_by") if final else None,
        "error": error.get("reason") if error else None,
        "priced_usd": round(sum(c["cost_usd"] for c in calls), 6),
        "ledger_problems": find_problems(steps),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes-spend", action="store_true", help="required: real free-tier calls")
    parser.add_argument("--max-requests", type=int, default=30)
    parser.add_argument("--scenarios", default="rung3,killed,fallback")
    args = parser.parse_args()

    if not args.yes_spend:
        sys.exit("refusing: pass --yes-spend (real free-tier requests)")
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("refusing: GEMINI_API_KEY is not set")
    if not any(f"@{host}" in settings.database_url for host in LOCAL_HOSTS):
        sys.exit("refusing: this harness only runs against a local database")

    requests = 0

    def pace(_request: httpx2.Request) -> None:
        nonlocal requests
        if requests >= args.max_requests:
            raise RequestCapReached(f"{requests} requests used")
        if requests:
            time.sleep(GAP_SECONDS)
        requests += 1

    http = httpx2.Client(timeout=settings.llm_timeout_seconds, event_hooks={"request": [pace]})
    real = GeminiLLMClient(client=http)
    lines: list[dict[str, Any]] = []
    try:
        for scenario in args.scenarios.split(","):
            line = run_scenario(scenario, real)
            lines.append(line)
            print(
                f"{line['run_id']} {scenario:<9} ok={line['ok']} asked={line['models_asked']} "
                f"repaired_by={line['repaired_by']} fallback_from={line['fallback_from']} "
                f"error={line['error']} problems={line['ledger_problems']}",
                flush=True,
            )
    except RequestCapReached as exc:
        print(f"stopped at the cap: {exc}")
    finally:
        out = {
            "conditions": {
                "database": "local Postgres (throwaway container)",
                "billed": "Gemini free tier, $0; priced_usd is the paid rate",
                "tools": "fake",
                "real_requests": requests,
            },
            "scenarios": lines,
        }
        RESULTS.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(f"{requests} real requests -> {RESULTS.name}")
        pool.close()


if __name__ == "__main__":
    main()
