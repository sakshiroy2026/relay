# Relay

**A durable execution runtime for LLM agents.**

An agent run must survive the worker process being killed at any moment, resume from the last completed step, and never duplicate a side effect.

Instead of an agent loop living in a process that dies when the process dies, every model call and every tool invocation is appended to a write-ahead ledger in PostgreSQL. If a worker crashes, is deployed over, or hits a rate limit, another worker picks the run up and continues from the exact step where it stopped — without re-issuing model calls already paid for, and without re-executing side effects already applied.

![Demo page: worker 20a728 was killed after step 3; worker 0f4d83 took over at step 4 and finished the run](learn/img/frontend-takeover.jpg)

*The demo page during run `783a03b3`: the worker that owned the run was killed with `docker kill` after step 3; another worker replayed the ledger and continued at step 4.*

---

## Status

**Under active development.** This section is updated at the end of every working day.

| Area | State |
|---|---|
| Ledger, leases, fencing, crash recovery | ✅ working, manually verified (evidence below) |
| Worker pool, `SKIP LOCKED` claiming | ✅ working, 3 workers |
| LLM client layer, output schemas, cost accounting | ✅ working, against a scripted fake model |
| Tools (`web_search`, `fetch_page` on canned fake data; `lookup_existing`, `save_company`, `flag_for_review` on Postgres), tool registry, system prompt | ✅ working |
| Agent loop inside the workers | ✅ runs end to end: request → worker → ledger → stored record |
| Transcript replay after a crash | ✅ working, verified with `docker kill` mid-run (evidence below) |
| Idempotent write tools (per-tool key + unique constraint) | ✅ working, verified with `docker kill` inside `save_company` (evidence below) |
| API key check, per-run budget cap | ✅ working (evidence below) |
| Live demo page (`GET /`, polls the ledger once a second) | ✅ working, kill-and-resume shown live (screenshot above) |
| SSRF guard on `fetch_page` (https only, resolved IPs must be public, redirects re-checked, max 3) | ✅ unit-tested (24 cases); DNS checks go live with the real fetcher |
| Chaos harness and measured results | ✅ 200 runs (results below) |
| Real model provider | 🔨 next (everything so far runs on a scripted fake model) |
| Public deployment | ⬜ planned |

---

## Measured results

`python -m chaos.crash_test --n 200`: each iteration starts a run, waits a random 0–10 s, runs `docker kill` on the container that owns the run, waits for the run to finish, and checks its ledger. The runs go one at a time.

| Metric | Result |
|---|---|
| Runs | 200 |
| Killed mid-run | 196 (the other 4 weren't claimed yet at the kill moment, so nothing was killed) |
| Killed runs that still finished `succeeded` | **196 / 196** |
| Model calls paid twice | **0** |
| Duplicate company rows | **0** |
| Write-tool effects committed more than once | **0** (13 `save_company` calls were re-issued after a kill and executed once) |
| Ledger rows replayed instead of redone | 1,212 (including 302 model calls not paid again) |
| Tool calls re-run after a kill landed mid-tool | 30 |
| Tokens a cold restart would have spent again | 492,938 of 1,440,982 in the killed runs (34%) |
| Time from kill to the next worker's first step | median 15.8 s, p90 16.9 s |

**Conditions:** a scripted fake model and fake search/fetch tools, with real PostgreSQL writes (Neon). 3 worker containers, a 15 s lease, and a 1.5 s fake model delay. The harness measures crash recovery, not answer quality, so it runs on the fake client. Time to resume tracks the lease setting; with the default 90 s lease it would be up to 90 s. Raw per-run data: [`chaos/iterations.jsonl`](chaos/iterations.jsonl); summary: [`chaos/results.json`](chaos/results.json); method and caveats: [`learn/CHAOS.md`](learn/CHAOS.md).

No accuracy numbers are published yet. They need a real model and a hand-labelled dataset.

---

## The reference workload

Relay ships with one concrete agent so it is a product, not a library: a **company enrichment agent**.

Give it a company domain. It researches the company across the web, extracts a structured record, checks for duplicates, writes the record, and flags ambiguous fields for human review rather than guessing.

```
POST /v1/runs  { "domain": "razorpay.com" }
      ↓
{ "name": "Razorpay", "hq_country": "India", "founded_year": 2014,
  "industry": "Fintech / Payments", "employee_range": "1001-5000",
  "sources": ["https://..."], "flagged_fields": ["employee_range"] }
```

The workload was chosen because it is multi-step (8–15 tool calls, so there is real work to lose), long enough that crashing mid-run is realistic, dependent on genuinely flaky external calls, and — critically — because it **writes records**, which is what makes idempotency a requirement rather than a decoration.

---

## Architecture

```
  client
    │  POST /v1/runs
    ▼
┌──────────────────────┐
│ API (FastAPI)        │  auth, validation, ONE transaction, 202 Accepted
│ never calls a model  │
└──────────┬───────────┘
           │ writes
           ▼
┌──────────────────────┐        ┌────────────────────────┐
│ PostgreSQL           │        │ Worker pool (×3)       │
│  runs   ← the queue  │◄───────│  claim (SKIP LOCKED    │
│  steps  ← the ledger │  claim │         + 90s lease)   │
│  companies, approvals│        │  replay ledger         │
└──────────────────────┘        │  agent loop            │
           ▲                    │  append step, heartbeat│
           └────────────────────┴────────────────────────┘
                                          │
                                          ▼
                              model provider · web search · page fetch
```

### Three decisions that shape everything

**1. The API never calls a model.** The API tier does auth, validation, one database transaction, and returns. p99 API latency is a database write, the API can be rate-limited independently of model spend, and a model outage cannot take down the API.

**2. The queue lives inside PostgreSQL.** Enqueueing a run and creating the run row happen in the same transaction, so there is no dual-write problem, no "message published but row not committed" failure mode, and no broker to operate. `SELECT … FOR UPDATE SKIP LOCKED` is exactly the row-claiming primitive a job queue needs.

**3. The ledger is four things at once.** The `steps` table is the durable execution log, the event stream, the observability trace, and the eval trace. One append-only table serving four purposes is why the system is small.

### The four mechanisms

| Mechanism | What it does |
|---|---|
| **Lease** | A claim on a run expires after 90 seconds unless the worker renews it. A dead worker needs no cleanup: the timestamp simply passes and the same query that claims fresh runs reclaims it. |
| **Fencing** | `UNIQUE (run_id, step_index)` on the ledger. A stalled worker that wakes after its lease expired and tries to write a step the new owner already wrote gets a unique violation, recognises it was fenced, and aborts. Split-brain protection from a database constraint instead of a distributed lock service. |
| **Idempotency** | Three overlapping layers: client `Idempotency-Key` (planned), a per-tool-invocation key written *before* execution, and a database uniqueness constraint on the output table (both built). The key is `<run_id>:<tool_call_id>`, so a re-run after a crash reuses it, and the write commits in the same transaction as the key's "done" mark. |
| **Replay** | A new worker rebuilds the conversation by reading the ledger in order. Not code re-execution — the transcript was written down, so recovery is a `SELECT … ORDER BY` and a loop. |

### The agent loop

The loop is the only thing that talks to both the model and the tools; they never talk to each other. Each turn it renews the lease, asks the model, and **writes the `model_call` step (with its cost) before judging the reply**, because the provider has already billed for it. For every tool the model requests, it writes a `tool_call` step *before* running the tool and a `tool_result` step after, so a crash between the two leaves a visible "started, not confirmed" marker instead of silence. A run ends with a validated record (`final`), a rejected answer or a 15-turn cap (`error`), or a lost lease, which hands the run back to the pool.

Before its first turn, the loop **replays the ledger**: one ordered `SELECT` over the run's steps rebuilds the conversation and gives the next free step index. A resumed run carries on from there. Tool calls that were logged but never confirmed are re-run before the model is asked anything, a run whose `final` or `error` is already written is not re-run, and the 15-turn cap counts turns already in the ledger.

The worker around it only claims, heartbeats and finishes. It records the outcome with an UPDATE guarded by `lease_owner = me AND status = 'running'`: a heartbeat only proves ownership at the moment it ran, so the finishing write re-checks ownership in the same statement. A worker whose lease was taken over while it was finishing cannot mark someone else's run as done.

---

## Verified so far

Manual tests against three worker containers and PostgreSQL on Neon. Run ids are from the project's own ledger.

**Agent loop end to end** — run `55d5c198`: `POST /v1/runs` → worker-2 claimed it at attempt 1 → 11 ledger rows, indexes 0–10, no gaps (`run_started`, three `model_call` turns, three `tool_call`/`tool_result` pairs including two tool calls requested in a single model reply, `final`) → run `succeeded` with the validated company record stored. Against the fake model and fake tools.

**Replay after a crash, mid-run** — run `7b0200c8`: worker-2 wrote steps 1–3 (the first model call and its search), then `docker kill`. Worker-3 claimed the run at attempt 2 once the 15 s test lease expired, rebuilt the conversation from the ledger, and wrote step 4: the *second* model reply, not the first one again. The run finished with exactly 3 `model_call` rows, the same as a run that never crashed, 11 rows 0–10, no gaps. Nothing was paid for twice.

**Replay after a crash, mid-tool** — run `efbc7f52`: worker-2 was killed after writing a `fetch_page` `tool_call` (step 5) and before its result, using a temporary 3 s delay in the fake fetch that isn't in the code. Worker-1 resumed, saw a tool call with no result, and re-ran it *before* asking the model anything: a new `tool_call` for the same call id at step 6, the result at step 7, then the remaining fetch. 3 `model_call` rows, 12 rows, no gaps. Re-running is safe here because both fetch tools only read. The next two runs show the write case.

**Exactly one company row after a crash inside `save_company`** — run `bf7b65a7`: worker-2 saved the company (the row and its `tool_invocations` receipt committed together at 11:58:46) and was killed before logging the `tool_result`. Worker-1 resumed, re-issued the same call with the same idempotency key, found the receipt marked done, and returned the stored result without writing. `SELECT count(*) FROM companies WHERE domain = …` = 1, the row was never updated, and the receipt shows `attempts = 2, executions = 1`.

**Crash before the write committed** — run `fa4340ca`: worker-2 recorded the key and was killed before its write. Worker-1 re-ran the call and did the write once. Again 1 row, `attempts = 2, executions = 1`. Both runs used temporary delays inside the tool to aim the kill; those delays aren't in the code.

`scripts/idempotency_smoke.py` exercises the same paths without workers: the same key twice is executed once, a second key for the same domain updates the one existing row, and a key left `in_flight` by a dead worker executes exactly once.

**API key** — every `/v1` route requires `Authorization: Bearer <key>`, compared in constant time. Without a key, with a wrong key, or without the `Bearer` scheme: `401`. With the key: `202`. `/healthz` stays open. If the server has no key configured, `/v1` answers `503` instead of running open.

**Per-run budget** — run `e8280abe`, created with a budget of $0.0001 (below the cost of one fake model call, $0.004881): the first model call ran, the next turn's check wrote a `budget_exceeded` step, and the run ended `failed` with exactly one `model_call` in its ledger. Spend is summed from the ledger's `model_call` rows, so the cap survives crashes and replays; it can overshoot by at most one call, whose cost is only known after it returns. A normal run (`272f1414`) costs $0.026 in fake prices against the default $0.25.

**Live demo page** — run `783a03b3`, started from the page at `GET /`: worker `20a728` wrote steps 1–3, was killed with the `docker kill` command the page displays, and worker `0f4d83` took over at step 4 (attempt 2). The page showed the takeover banner, the per-worker badges, the final record and the saved company row. The page is one static HTML file polling `GET /v1/runs/{id}` and `GET /v1/runs/{id}/steps` once a second; it holds no API key and renders ledger text as plain text only.

`scripts/check_run.py` checks every run above for index gaps and repeated model calls. The "already finished" and "already failed" replay paths are covered by unit tests (`tests/test_replay.py`) but haven't been hit live.

**Crash recovery (placeholder workload, before the agent loop)** — run `01ab701a`: worker-3 wrote steps 1–5, then `docker kill` (exit 137). Worker-1 claimed the run at attempt 2 once the lease expired, resumed at step 6, and finished. Ledger 0–6, no gaps, no repeated steps.

**Fencing under a simulated stall** — run `e9bcda30`: worker-1 wrote steps 1–2, then `docker pause`. Worker-3 claimed the run 18s later at attempt 2 and wrote steps 3–6. On `docker unpause`, worker-1 attempted step 3, hit the unique constraint, logged `lease_lost`, wrote nothing further, and stayed healthy.

**Claim exclusivity**: 50 runs drained by 3 workers — every run processed exactly once, no run with two distinct lease owners.

All of these were run by hand, one run at a time, against the fake model and fake tools. The `01ab701a` and `e9bcda30` tests predate the agent loop. The 200-run chaos harness (see Measured results) repeats the kill test with random kill points.

---

## Deliberately excluded

| Not used | Why |
|---|---|
| Kubernetes | One service, three worker processes, one box. |
| Kafka | One producer, one consumer group; the ledger already provides replay. |
| Celery / RQ | They would hide the exact mechanism this project exists to demonstrate. |
| LangChain / LangGraph | The loop is the deliverable; a framework would obscure it. |
| Vector database | There is no corpus. Retrieval is live web search. |
| React | The UI is a form and a log tail. |

Relay is also **not a Temporal clone**. It is a deliberately minimal, Postgres-only subset of durable execution. Temporal replays your *code*, and therefore has to handle non-determinism and sandboxing; Relay replays the *transcript*, which is far simpler and gives up the ability to resume mid-model-call.

---

## Stack

Python 3.13 · FastAPI · Pydantic v2 · psycopg 3 (raw SQL, no ORM) · Alembic · PostgreSQL (Neon) · Docker Compose

Planned: Redis, OpenTelemetry, Prometheus, Grafana, Caddy, AWS EC2.

**On model spend:** development runs against a scripted fake model client and fake tools (canned search results and pages) behind the same `LLMClient` interface a real provider will use, so durability work costs nothing to test. The fake is deterministic and chooses its reply from the conversation it is handed rather than from internal state — meaning a fresh worker resuming after a crash gets the same reply the dead one would have. The chaos harness ran on the fake client, because it measures crash recovery rather than model quality; this is stated next to the numbers.

---

## Roadmap

Currently implements durable execution, lease-based claiming with fencing, transcript replay, tool-level idempotency with a database backstop, an API key and per-run budget, a live demo page, and measured chaos-harness results.

Next, in order: the switch from the fake model to a real provider (with real search and page fetch); public deployment; then a hand-labelled golden dataset with LLM-as-judge evaluation gated in CI.

Later: per-tool circuit breakers, a schema-repair ladder with model escalation, and OpenTelemetry GenAI tracing.
