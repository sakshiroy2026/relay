"""Prometheus metrics for Relay, read straight from the ledger (Day 17).

WHAT IT IS  "The steps table is the observability trace" made literal: a
            custom Prometheus collector that answers every scrape with a few
            aggregate queries over runs/steps/tool_invocations/
            circuit_breakers. Nothing is instrumented in the hot path and no
            counters can drift or be lost in a crash — the database is the
            counter. Plus one real-time histogram of API request latency,
            which only the process can know.
GOES IN     A scrape of GET /metrics. snapshot() for the ops dashboard.
COMES OUT   Text exposition: relay_runs, relay_steps, relay_spend_usd,
            relay_tokens, relay_tool_calls, relay_tool_retry_attempts,
            relay_breaker_state, relay_repairs, relay_dlq_depth,
            relay_resumed_runs, relay_open_leases, relay_http_* and more.
TOUCHES     The database, read-only: ~6 aggregate queries per scrape (indexed
            or full-scan over steps — fine at this scale; at real scale the
            scrape interval is the knob, or a materialized rollup).
FAILS WHEN  The database is down -> the scrape fails and Prometheus records
            the target down, which is itself the alert.
"""

import time
from collections.abc import Iterator
from typing import Any

from prometheus_client import Histogram
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import REGISTRY, Collector

from relay.db import pool

HTTP_LATENCY = Histogram(
    "relay_http_request_seconds",
    "API request latency",
    ["method", "route", "status"],
    buckets=(0.005, 0.025, 0.1, 0.5, 2.5),
)

BREAKER_STATES = ("closed", "open", "half_open")


def _rows(sql: str) -> list[tuple[Any, ...]]:
    with pool.connection() as conn:
        return list(conn.execute(sql).fetchall())


def snapshot() -> dict[str, Any]:
    """One dict with everything the ops dashboard shows. Shared with the collector."""
    runs = {
        status: int(n)
        for status, n in _rows("SELECT status::text, count(*) FROM runs GROUP BY status")
    }
    steps = {
        kind: int(n) for kind, n in _rows("SELECT kind::text, count(*) FROM steps GROUP BY kind")
    }
    (spend, tokens_in, tokens_out) = _rows(
        "SELECT COALESCE(sum(spent_usd), 0), COALESCE(sum(tokens_in), 0), "
        "COALESCE(sum(tokens_out), 0) FROM runs"
    )[0]
    resumed, open_leases = _rows(
        "SELECT count(*) FILTER (WHERE attempt_count > 1 AND status = 'succeeded'), "
        "count(*) FILTER (WHERE status = 'running' AND lease_expires_at > now()) FROM runs"
    )[0]
    tool_calls = {
        (tool, "ok" if ok else "failed"): int(n)
        for tool, ok, n in _rows(
            "SELECT payload->>'tool', (payload->>'ok')::boolean, count(*) "
            "FROM steps WHERE kind = 'tool_result' GROUP BY 1, 2"
        )
    }
    retry_attempts = {
        str(a): int(n)
        for a, n in _rows(
            "SELECT COALESCE(payload->>'attempts', '1'), count(*) "
            "FROM steps WHERE kind = 'tool_result' GROUP BY 1"
        )
    }
    breakers = {tool: state for tool, state in _rows("SELECT tool, state FROM circuit_breakers")}
    repaired = {
        tag: int(n)
        for tag, n in _rows(
            "SELECT jsonb_array_elements_text(payload->'repaired_by'), count(*) "
            "FROM steps WHERE kind = 'final' AND payload ? 'repaired_by' GROUP BY 1"
        )
    }
    errors = {
        reason: int(n)
        for reason, n in _rows(
            "SELECT payload->>'reason', count(*) FROM steps WHERE kind = 'error' GROUP BY 1"
        )
    }
    executions = _rows(
        "SELECT COALESCE(sum(executions), 0), COALESCE(sum(attempts), 0), "
        "count(*) FILTER (WHERE executions > 1) FROM tool_invocations"
    )[0]
    return {
        "runs": runs,
        "steps": steps,
        "spend_usd": float(spend),
        "tokens": {"in": int(tokens_in), "out": int(tokens_out)},
        "resumed_runs": int(resumed),
        "open_leases": int(open_leases),
        "tool_calls": {f"{t}/{s}": n for (t, s), n in tool_calls.items()},
        "tool_retry_attempts": retry_attempts,
        "breakers": breakers,
        "repaired_finals": repaired,
        "error_reasons": errors,
        "write_effects": {
            "executed": int(executions[0]),
            "asked": int(executions[1]),
            "executed_twice": int(executions[2]),  # the number that must stay 0
        },
        "dlq_depth": runs.get("dead", 0) + runs.get("failed", 0),
        "generated_at": time.time(),
    }


class LedgerCollector(Collector):
    def collect(self) -> Iterator[Metric]:
        s = snapshot()

        runs = GaugeMetricFamily("relay_runs", "Runs by status", labels=["status"])
        for status, n in s["runs"].items():
            runs.add_metric([status], n)
        yield runs

        steps = CounterMetricFamily("relay_steps", "Ledger rows by kind", labels=["kind"])
        for kind, n in s["steps"].items():
            steps.add_metric([kind], n)
        yield steps

        yield GaugeMetricFamily("relay_spend_usd", "Total model spend (USD)", value=s["spend_usd"])
        tokens = CounterMetricFamily("relay_tokens", "Model tokens", labels=["direction"])
        tokens.add_metric(["in"], s["tokens"]["in"])
        tokens.add_metric(["out"], s["tokens"]["out"])
        yield tokens

        yield GaugeMetricFamily(
            "relay_resumed_runs", "Runs that succeeded on attempt > 1", value=s["resumed_runs"]
        )
        yield GaugeMetricFamily(
            "relay_open_leases", "Runs currently owned by a live lease", value=s["open_leases"]
        )
        yield GaugeMetricFamily(
            "relay_dlq_depth", "Failed + dead runs (requeue candidates)", value=s["dlq_depth"]
        )

        tools = CounterMetricFamily(
            "relay_tool_calls", "Tool results by tool and outcome", labels=["tool", "outcome"]
        )
        for key, n in s["tool_calls"].items():
            tool, outcome = key.rsplit("/", 1)
            tools.add_metric([tool, outcome], n)
        yield tools

        retries = CounterMetricFamily(
            "relay_tool_retry_attempts", "Tool results by attempts used", labels=["attempts"]
        )
        for attempts, n in s["tool_retry_attempts"].items():
            retries.add_metric([attempts], n)
        yield retries

        breaker = GaugeMetricFamily(
            "relay_breaker_state", "1 for the tool's current state", labels=["tool", "state"]
        )
        for tool, state in s["breakers"].items():
            for candidate in BREAKER_STATES:
                breaker.add_metric([tool, candidate], 1 if state == candidate else 0)
        yield breaker

        repairs = CounterMetricFamily(
            "relay_repairs", "Final answers salvaged, by rung", labels=["rung"]
        )
        for tag, n in s["repaired_finals"].items():
            repairs.add_metric([tag], n)
        yield repairs

        errors = CounterMetricFamily("relay_errors", "Error steps by reason", labels=["reason"])
        for reason, n in s["error_reasons"].items():
            errors.add_metric([reason], n)
        yield errors

        yield GaugeMetricFamily(
            "relay_write_effects_executed_twice",
            "Idempotency keys whose effect committed more than once (must be 0)",
            value=s["write_effects"]["executed_twice"],
        )


_registered = False


def register() -> None:
    global _registered
    if not _registered:
        REGISTRY.register(LedgerCollector())
        _registered = True
