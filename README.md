# Relay

**A durable execution runtime for LLM agents.**

An agent run must survive the worker process being killed at any moment, resume from the last completed step, and never duplicate a side effect.

Instead of an agent loop living in a process that dies when the process dies, every model call and every tool invocation is appended to a write-ahead ledger in PostgreSQL. If a worker crashes, is deployed over, or hits a rate limit, another worker picks the run up and continues from the exact step where it stopped — without re-issuing model calls already paid for, and without re-executing side effects already applied.

---

## Status

**Under active development.** This section is updated at the end of every working session.

| Area | State |
|---|---|
| Ledger, leases, fencing, crash recovery | ✅ working, manually verified (evidence below) |
| Worker pool, `SKIP LOCKED` claiming | ✅ working, 3 workers |
| LLM client layer, output schemas, cost accounting | ✅ working, against a scripted fake model |
| Agent loop and tools | 🔨 in progress |
| Transcript replay after a crash | ⬜ next |
| Idempotent write tools | ⬜ planned |
| Public deployment | ⬜ planned |
| Chaos harness and measured results | ⬜ planned |

No measured reliability or accuracy numbers are published yet. They will appear here when the chaos harness and eval suite have actually produced them.

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
| **Idempotency** | Three overlapping layers: client `Idempotency-Key`, a per-tool-invocation key written *before* execution, and a database uniqueness constraint on the output table. |
| **Replay** | A new worker rebuilds the conversation by reading the ledger in order. Not code re-execution — the transcript was written down, so recovery is a `SELECT … ORDER BY` and a loop. |

---

## Verified so far

Manual tests against three worker containers and PostgreSQL on Neon. Run ids are from the project's own ledger.

**Crash recovery** — run `01ab701a`: worker-3 wrote steps 1–5, then `docker kill` (exit 137). Worker-1 claimed the run at attempt 2 once the lease expired, resumed at step 6, and finished. Ledger 0–6, no gaps, no repeated steps.

**Fencing under a simulated stall** — run `e9bcda30`: worker-1 wrote steps 1–2, then `docker pause`. Worker-3 claimed the run 18s later at attempt 2 and wrote steps 3–6. On `docker unpause`, worker-1 attempted step 3, hit the unique constraint, logged `lease_lost`, wrote nothing further, and stayed healthy.

**Claim exclusivity**: 50 runs drained by 3 workers — every run processed exactly once, no run with two distinct lease owners.

These were run by hand. The scripted 200-run chaos harness that turns them into published numbers is planned, not built.

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

**On model spend:** development runs against a scripted fake model client behind the same `LLMClient` interface a real provider will use, so durability work costs nothing to test. The fake is deterministic and chooses its reply from the conversation it is handed rather than from internal state — meaning a fresh worker resuming after a crash gets the same reply the dead one would have. When the chaos harness runs, it will run on the fake client, because it measures crash recovery rather than model quality; that will be stated alongside the numbers.

---

## Roadmap

Next, in order: the agent loop over five hardcoded tools; transcript replay on claim; idempotent write tools; an API key check and a per-run cost cap; public deployment; then the chaos harness and a hand-labelled golden dataset with LLM-as-judge evaluation gated in CI.

Later: SSRF hardening on the page-fetch tool, per-tool circuit breakers, a schema-repair ladder with model escalation, and OpenTelemetry GenAI tracing.
