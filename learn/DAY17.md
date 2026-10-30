# Day 17 — Observability: Prometheus metrics, OpenTelemetry traces, the ops page

## 1. The picture

The thesis since Day 1 was "the steps table is four things at once: execution
log, event stream, observability trace, eval trace". Day 17 cashes in the
observability part. Instead of instrumenting the hot path with counters that
drift and die with the process, one Prometheus *collector* answers every scrape
by asking the database — the ledger **is** the counter, so a crashed worker
loses no metrics, and the numbers always agree with the data.

**The hospital one more time:** management doesn't make nurses carry tally
counters. The charts already hold everything; the morning report is *compiled
from the charts*. Relay's `/metrics` endpoint is the morning report, compiled
at scrape time.

Tracing is the one thing the ledger doesn't give: the standard shape other
tools speak. Each run attempt becomes an OpenTelemetry trace (child spans per
model call with GenAI attributes, per tool dispatch), and — the satisfying bit —
the `steps.trace_id` column that migration 001 reserved on Day 1 finally gets
written. A ledger row and its trace point at each other.

## 2. Each file's job

| File | One line |
|---|---|
| `relay/obs/metrics.py` (new) | `LedgerCollector`: ~6 aggregate queries per scrape → `relay_runs`, `relay_steps_total`, `relay_spend_usd`, `relay_tokens_total`, `relay_resumed_runs`, `relay_dlq_depth`, `relay_tool_calls_total`, `relay_tool_retry_attempts_total`, `relay_breaker_state`, `relay_repairs_total`, `relay_errors_total`, `relay_write_effects_executed_twice`; plus `relay_http_request_seconds` (the one thing only the process knows) |
| `relay/obs/tracing.py` (new) | `setup(service)` once per process; real trace ids always, OTLP/HTTP export only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (the collector config in the repo's `otel-collector-config.yaml` is where it will point in deployment) |
| `relay/worker.py` | A `relay.run_attempt` span per claim (run id, attempt, worker, outcome) |
| `relay/agent/loop.py` | Child spans: `relay.model_call` (GenAI attrs: model, input/output tokens, attempts) and `relay.tool <name>` (ok, attempts) |
| `relay/core/ledger.py` | `append_step` writes the active span's trace id into `steps.trace_id` (NULL outside a span — scripts stay honest) |
| `relay/api/main.py` | `/metrics` (Prometheus text), `/ops` + `/ops.json` (the dashboard), a latency middleware that labels by **route template** (`/v1/runs/{run_id}`), never the raw path |
| `relay/api/static/ops.html` (new) | The screenshot-able view: cards + tables polling `/ops.json` every 2 s, same styling as the demo page, no key inside |

New dependencies (first since Day 1, reason recorded here): `prometheus-client`
(the exposition format), `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http`
(spans + the standard export). All three are libraries, not infrastructure.

## 3. The design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| Metrics computed from the ledger at scrape time | Counters incremented in workers | Counters in process memory die with the process and drift from the truth; Relay's whole point is that the database already knows. Zero hot-path instrumentation, and the metrics survive every `kill -9` the chaos harness throws |
| One custom collector | `Gauge.set()` loops on a timer | A collector runs exactly when scraped — no background thread, no staleness window to reason about |
| HTTP latency labeled by route template | The raw path | `/v1/runs/{uuid}` as a label would create one time series per run — unbounded cardinality, the classic Prometheus self-DoS |
| A TracerProvider always, an exporter only if configured | Tracing only when a collector exists | Real trace ids cost microseconds and make `steps.trace_id` real *today*; the OTLP endpoint is one env var away when the collector exists (deployment) |
| One trace per run **attempt**, not per run | Resurrect the dead attempt's trace context | A reclaimed run is a genuinely new execution by a new process; the ledger's `run_id` already ties the attempts together. Propagating context through a crash would be theater |
| `/metrics`, `/ops` open like `/healthz` | Behind the API key | They expose counts and states, never payloads, domains or keys; scrapers and dashboards shouldn't hold tenant credentials |
| The ops page polls `/ops.json`, not `/metrics` | Parse Prometheus text in JS | The snapshot dict is the same data the collector uses — one source, two formats |

## 4. The proofs

- A worker-driven run: all 13 worker-written ledger rows carry the **same
  32-hex trace id**, `run_started` (written by the API-less harness) stays NULL.
- `/metrics` on the dev database after today's harnesses: 38 `relay_*` series —
  including `relay_resumed_runs 145`, `relay_repairs_total{rung="extracted_json"} 60`,
  `relay_errors_total{reason="model_unavailable"} …`, and
  `relay_write_effects_executed_twice 0` on 4,300+ runs of torture-test history.
- The screenshot in the README (`learn/img/ops-dashboard.png`): the `/ops` page
  over that same history — runs, crash-resumes, spend, DLQ depth, the repair
  ladder's rungs, and the must-stay-zero card, all compiled from the ledger.
- 5 new tests (104 total): the families exist, the route template (not the raw
  path) is the label, `/ops.json` holds the dashboard's contract with
  `executed_twice == 0` on live data, and a span's trace id ends up on the
  ledger row written inside it.

## 5. Questions to answer yourself

1. Why is computing metrics at scrape time *more* correct here than counters,
   when every monitoring guide teaches counters?
2. What breaks if the latency histogram is labeled with the raw URL path?
3. Why does a reclaimed run get a new trace instead of continuing the old one?
4. Why is `relay_write_effects_executed_twice` a metric at all, when tests
   already assert it's zero?
5. Why do `/metrics` and `/ops` skip the API key when everything else needs it?
6. What's the scaling story when `steps` has 100 million rows and a scrape's
   aggregate queries get slow?

<details>
<summary>Answers</summary>

1. Counters count *what the process saw*; the ledger records *what happened*.
   With workers being killed on purpose, in-process counters would undercount
   every time. Scrape-time aggregation can't disagree with the data because it
   IS the data.
2. One label value per run id → one time series per run → Prometheus memory
   grows without bound and queries die. Label values must come from a small
   fixed set; the route template is exactly that.
3. The old process is dead and its context died with it; pretending otherwise
   fakes causality. The honest join is in the data: both attempts' rows share
   `run_id`, each attempt's rows share a `trace_id`.
4. Tests prove it on test days; the metric watches it in production, forever,
   and an alert on `> 0` is the single most important alarm this system can
   have — it means the idempotency guarantee broke.
5. They expose only aggregate counts and states. Requiring a tenant key would
   put a credential into every scraper and dashboard config — a bigger risk
   than the data exposed. (In deployment they'd also bind to an internal port.)
6. The scrape does ~6 GROUP BYs; at large scale you widen the scrape interval,
   add partial indexes per kind, or maintain a small rollup table the workers
   update — the collector's interface doesn't change.
</details>

## 6. Interview lines

- "I didn't instrument the workers — I made the ledger the metrics source: a
  custom Prometheus collector aggregates it at scrape time, so the numbers
  survive every kill -9 and can never disagree with the data. The one alert
  that matters is `relay_write_effects_executed_twice > 0`."
- "Each run attempt is an OpenTelemetry trace with GenAI-attribute spans per
  model call, and the trace id is written onto every ledger row — the column
  existed from day one; the schema was designed for observability before the
  observability existed."
- "The latency middleware labels by route template, never the raw path —
  per-run-id label values would be unbounded cardinality, which is how people
  DoS their own Prometheus."
