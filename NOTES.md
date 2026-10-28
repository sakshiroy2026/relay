# Relay — Build Notes

## Day 1 — Skeleton

**Built:** Project scaffold, Neon Postgres, migration 001 (tenants, api_keys,
runs, steps), seed script, FastAPI with POST /v1/runs, GET /v1/runs/{id},
/healthz, /readyz.

**Decisions worth remembering:**

1. **The run row and step 0 are written in ONE transaction.** psycopg opens a
   transaction on the first statement and commits when the `with pool.connection()`
   block exits. So you can never have a run without its opening step, or a step
   pointing at a run that doesn't exist. This is also why the job queue lives in
   Postgres rather than Redis — enqueueing and creating state are the same commit,
   so there's no dual-write problem.

2. **The API never calls a model.** It validates, writes, returns 202. Everything
   expensive happens later in a worker. Consequence: a model outage cannot take
   down the API, and p99 API latency is just a database write.

3. **Three schema constraints carry the project:**
   - `UNIQUE(run_id, step_index)` on `steps` — does nothing yet, becomes the
     fencing token on Day 3. Split-brain protection from a DB constraint instead
     of a lock service.
   - Partial index `runs_claimable_idx ... WHERE status IN ('pending','running')`
     — only indexes rows a worker could claim, so the claim query stays fast as
     completed runs pile up.
   - `awaiting_approval` in the `run_status` enum is deliberately absent from that
     index predicate — that's what lets a parked run consume zero compute.

4. **No ORM, raw SQL via psycopg.** The whole project is about SQL semantics
   (SKIP LOCKED, unique-constraint fencing, transaction boundaries). An ORM would
   hide exactly what's being demonstrated. Alembic still needs SQLAlchemy Core to
   run migrations — that's a migration-tool dependency, not an application one.

**Gotchas hit:** setuptools flat-layout package discovery (fixed with
`[tool.setuptools.packages.find] include = ["relay*"]`), psycopg needs `Jsonb()`
rather than `json.dumps()` for jsonb columns, Alembic needs `script.py.mako`
present to generate revisions.

## Day 2 — claim query
- relay/worker.py: claim (UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)) → 2s fake work → mark succeeded (guarded by lease_owner).
- Claim commits before work: row lock lasts ms, the lease timestamp protects the run afterwards.
- Worker survives psycopg.OperationalError (network blips, PoolTimeout): log, wait 2s, keep looping. Other exceptions still crash.
- Drain test: 50 runs, 3 workers → 50 succeeded, 0 claimed twice, split 17/16/17.
- attempt_count is the "claimed once" proof; lease_owner only keeps the last owner.
- Gotcha: RELAY_API_KEY in .env broke Settings → extra="ignore" in relay/config.py.

## Day 3a
- Added relay/core/ledger.py: LeaseLost, next_step_index (read once = worker's belief), append_step (UniqueViolation → LeaseLost).
- Worker: extend_lease heartbeat, 6×3s fake steps, lease_lost handling, LEASE_SECONDS from .env (15 for tests).
- Scripts: ledger_smoke.py (fencing by hand), show_steps.py (ledger viewer + gap check).
- Crash test passed: A Ctrl+C'd after step 3 → B claimed at attempt 2, continued at step 4, no gaps. Stranded run rescued later by a fresh worker.
- Next: Day 3b — Dockerfile, 3 worker containers, docker kill + docker pause fencing test.

## Day 3b — Docker + fencing test
- First Dockerfile (python:3.13-slim, pip install ., exec-form CMD, PYTHONUNBUFFERED) + .dockerignore
- docker-compose.yml: `worker` service, build ., env_file .env, 3 replicas → relay-worker-1..3
- Worker id inside a container = container short id + ":1" (hostname:pid; pid is 1)
- docker kill test: run 01ab701a — killed after step 5 (Exited 137), other worker resumed at step 6, attempts=2, no gaps
- Fencing test: run e9bcda30 — worker-1 paused after step 2, worker-3 claimed after ~18s and wrote 3–6;
  on unpause worker-1 logged `lease_lost … step 3 already written`, wrote nothing, stayed Up
- Scripted fencing test deferred to the Day 15 chaos harness


## Day 4 — LLM client, fake model, pricing, schemas
- One plug shape (`Response`, `LLMClient` Protocol); providers are adapters. Ledger stores OUR shape.
- FakeLLMClient picks its reply by counting assistant turns in `messages`, not with a counter —
  so a fresh worker after a crash gets the same reply the dead one would have.
- Cost is computed per call and paid even when the reply fails validation.
- Anything with HttpUrl goes to JSON via `model_dump(mode="json")`; dataclasses via `asdict`.
- smoke test: good reply ACCEPTED, founded_year 2030 REJECTED, both cost $0.001689.

## Day 5 — tools, agent loop, worker wiring

**Built:** fake tools + registry (`tools.py`), the fake model's script (`fake_script.py`),
`make_llm()`, `SYSTEM_PROMPT`, the agent loop (`loop.py`), and the worker now runs the loop.
First end-to-end run `55d5c198`: 11 ledger rows (0–10), status `succeeded`, Acme record in `result`.

### The agent loop (`run_agent`)
1. Each turn: heartbeat ("still mine?") → ask the model → write a `model_call` row with its cost.
2. The `model_call` row is written BEFORE checking the reply, because the provider has already charged for it.
3. For each tool the model asks for: write `tool_call` (intent) → run the tool → write `tool_result`.
   Writing intent first means a crash leaves a visible "started, not confirmed" marker instead of silence.
4. It ends four ways: valid record → `final` + dict; invalid record → `error` + None;
   15 turns used → `error` + None; lease lost → raises `LeaseLost`.
5. Weak spot today: `messages` lives only in memory, so a worker that reclaims the run starts
   the conversation from `[user]` again and re-pays for model calls. Day 6 replay fixes this.

### The wiring (`run_once` in worker.py)
- Claim → read `domain` from `run["input"]["domain"]` → define `beat()` = `extend_lease(run_id)`
  → pass `beat` to `run_agent` (passed as a value, called later by the loop).
- Only `run_agent` sits inside `try`; `except LeaseLost` directly under it → a lost run never reaches the finish code.
- dict → `mark_succeeded(run_id, result)`; None → `mark_failed(run_id)`. Both guarded by
  `lease_owner = me AND status = 'running'`.
- Why guard if the loop heartbeats? The heartbeat proves "mine at that moment" only. Without the guard,
  a frozen worker A waking up would stamp `succeeded` over a run B now owns, and B would quit at its next heartbeat.

### Crash points
- Dies after `model_call`: ledger safe, that call is paid. Today the next worker re-pays (no replay yet).
- Dies between `tool_call` and `tool_result`: "about to run, unknown whether it happened".
  Fine for read-only `web_search`; dangerous for `save_company` → Day 7 idempotency.

### Owed
- `MAX_TURNS = 2` sabotage (never observed).
- Say the 5 loop sentences from memory at the start of Day 6.

### Gotchas learned
- Port 8000 is taken by another Python program → API on `--port 8001`.
- A rebuild ends `logs -f` → restart it. Same worker ids after a rebuild = the edit wasn't saved.
- `show_steps` takes the run id, not the worker id.

## Day 6 — Replay

See learn/DAY6.md. Explain-back owed.
Proofs: kill mid-run `7b0200c8` (resumed at step 4, 3 `model_call`, 11 rows),
kill mid-`fetch_page` `efbc7f52` (new `tool_call` row for the re-run, 12 rows).

## Day 7 — Write tools + idempotency

See learn/DAY7.md. Explain-back owed.
Proofs: kill after `save_company` committed `bf7b65a7` and before it committed `fa4340ca`:
1 company row each, receipt `attempts = 2, executions = 1`.

## Safety slice — API key + budget cap

See learn/SAFETY.md. Explain-back owed.
Proofs: 401 without / with a wrong key, 202 with the key; budget run `e8280abe`
(1 `model_call`, then `budget_exceeded`, `failed`).

## Demo page

See learn/FRONTEND.md. Explain-back owed.
Proof: run `783a03b3` started from the page, owner killed after step 3, takeover banner
at step 4, succeeded (screenshot `learn/img/frontend-takeover.jpg`).

## Chaos harness

See learn/CHAOS.md. Explain-back owed.
200 runs, 196 killed mid-run, 196 succeeded; 0 repeated model calls, 0 duplicate companies,
0 write effects committed twice; median resume 15.8 s (lease 15 s, fake delay 1.5 s).

## SSRF guard (Day 12, simple version)

See learn/SSRF.md. Explain-back owed.
`relay/agent/ssrf.py`: https only, resolved IPs must be public, redirects re-checked (max 3).
24 unit tests with a stub DNS. The real fetcher must use it with DNS + 2 MB / 10 s caps.

## Day 8 — Retries, backoff, per-tool circuit breaker

Built by Claude Code; see learn/DAY8.md. Explain-back owed.
Retries (full jitter, max 3) live INSIDE a step; breaker state is a Postgres row shared
by all workers (migration 003). Proofs: 50/50 flaky runs succeeded (41 calls recovered
by retry), breaker opened after 5 consecutive failures and self-closed via one probe,
kill -9 mid-retries resumed clean (run `28ca2373`). Numbers: chaos/fault_results.json
(run on local Postgres in a cloud container, not Neon).

## Day 16 — Idempotency, fencing and throughput, measured

Built by Claude Code; see learn/DAY16.md. Explain-back owed.
No mechanism changed; three harnesses leaned on the proven ones. A2: 500 keys
raced by 4 processes + 200 kill -9 mid-write -> every key executed once, receipt
and row never disagreed. A3: owner SIGSTOPped 100x -> 0 stale finishes (fenced by
the constraint 81x, heartbeat 14x, guarded finish 2x); 16 runs where the zombie's
write landed first and the NEW owner was fenced — still one valid history.
A4: claim query 0.111 ms with 100k finished runs (8.4 ms without the partial
index); 16 workers at 0.2 s model latency = 15.6x speedup (98% efficiency).
check_run.py now validates ledger shape too. Numbers: chaos/*_results.json
(local Postgres in a cloud container, not Neon).

## Day 9 — Schema repair ladder + dead-letter queue

Built by Claude Code; see learn/DAY9.md. Explain-back owed.
Ladder: strict parse -> free JSON extraction (brace-counting, handles fences AND
prose) -> ONE re-ask where the model sees its own validation errors as a real,
replayable turn (new step kind repair_requested, migration 004). 75/75 repairable
broken finals saved; 0 repairs on clean runs; hopeless answers fail after exactly
one re-ask. Rung 3 (stronger model) waits for the real-model switch.
DLQ: idle workers reap lease-expired runs with attempts spent -> status 'dead'
(unused since Day 1, now real); scripts/dlq.py lists by reason and requeues.
A requeue appends a 'requeued' ledger marker FIRST (clears the verdict in replay),
then flips to pending with max_attempts = attempts + 5 — so requeued runs RESUME:
24/24 model-outage failures requeued and finished without re-paying pre-outage
calls; 5/5 kill-9-until-exhausted runs reaped, requeued, succeeded.
Numbers: chaos/repair_dlq_results.json (local Postgres, not Neon). 20 new tests (92).

## Day 10 — Full API keys, tenant scoping, rate limits

Built by Claude Code; see learn/DAY10.md. Explain-back owed.
Keys are rly_<prefix>.<secret>: indexed prefix lookup, bcrypt check, Principal
(tenant + limits); verified keys cached 60 s as SHA-256 digests (294 ms cold ->
7 ms cached, measured). Env key still works -> dev tenant. All API SQL moved to
a repository layer where tenant_id is a required argument; cross-tenant reads
404 (not 403). Migration 005: per-tenant requests_per_minute (in-process window,
429) and max_concurrent_runs (Postgres advisory lock + count: 8 racing creates
vs cap 3 -> exactly 3 rows). relay/api/auth.py deleted; seed.py --tenant NAME.
7 new tests (99 total).

## Day 17 — Observability: Prometheus, OpenTelemetry, the /ops page

Built by Claude Code; see learn/DAY17.md. Explain-back owed.
No counters in the hot path: a custom Prometheus collector aggregates the LEDGER
at scrape time (38 relay_* series: runs, resumes, spend, retries, breaker state,
repair rungs, DLQ depth, and relay_write_effects_executed_twice — the one alert
that matters). HTTP latency labeled by route template (cardinality). OTel: one
trace per run ATTEMPT, GenAI spans per model call/tool; steps.trace_id (reserved
since migration 001) finally written — verified: all 13 worker rows of a run
share one trace id. OTLP export is one env var when a collector exists.
/ops dashboard (static HTML -> /ops.json) screenshot in README, taken over this
container's own 4,300-run test history. New deps (first since Day 1):
prometheus-client, opentelemetry-sdk, otlp-http exporter. 5 new tests (104).

## Day 11 — SSE: the ledger pushed, not polled

Built by Claude Code; see learn/DAY11.md. Explain-back owed.
Migration 006: AFTER INSERT ON steps -> pg_notify (fires on COMMIT: a fenced
zombie's rolled-back insert rings nothing). GET /v1/runs/{id}/events holds one
LISTEN connection and on each bell RE-READS run+steps via the tenant-scoped repo
(bell says "look again"; the ledger stays the only truth), emits update on a
fingerprint change, done at terminal. Page uses fetch-streaming (EventSource
can't send Authorization) with automatic fallback to the old polling.
Measured: 65 steps over 5 live runs, median 3.7 ms / p90 6.7 ms insert-commit ->
parsed event (vs ~500 ms avg at 1 s polling); Playwright browser run clean.
5 new tests (109 total).

## Client Idempotency-Key (the first idempotency layer)

Built by Claude Code; see learn/IDEMPOTENCY_KEY.md. Explain-back owed.
Migration 007: idempotency_keys (tenant_id, key) -> run_id + body sha256. The key
row commits with the run and step 0; the Day 10 per-tenant advisory lock already
serialises racing retries. Same key + same body -> same run, header
Idempotent-Replayed: true (checked BEFORE the cap: a replay creates nothing);
same key + other body -> 422; malformed -> 400. Demo page sends a fresh UUID per
Start. Live: run 4f186a0d replayed, 1 row. 7 new tests (116 total).

## Real tools (P2): Tavily search + pinned-IP fetcher

Built by Claude Code; see learn/REAL_TOOLS.md. Explain-back owed.
TOOLS_PROVIDER=real swaps in tavily_search + fetch_page under the SAME names and
schemas (same model menu, same cache prefix); refuses to boot without
TAVILY_API_KEY. Stdlib only, no new deps. fetch_page connects to the IP
check_url APPROVED (never re-resolves = DNS rebinding closed) while TLS still
verifies the URL's hostname; redirects re-checked (max 3), 2 MB cap, one 10 s
budget for the whole chain, Accept-Encoding identity (no zip bombs), text pages
only, text cut to page_text_max_chars=8000. Failures keep the Day 8 split.
32 tests (148 total), no network: stub DNS/HTTP, a local TLS server, and a
mutation check (re-resolving before connect fails the pinning test).
UNVERIFIED until the first real call: Tavily's wire format (docs host blocked
from the container). The pinned fetcher bypasses HTTP proxies by design.

## P1 — Real model adapter (built for $0, paid proof pending)

Built by Claude Code; see learn/P1_REAL_MODEL.md. Explain-back owed.
relay/llm/anthropic_client.py behind the same LLMClient protocol (official SDK
2.x, SDK retries off). Haiku 5.5 thinks, and a thinking signature is only valid
in a byte-identical conversation, so model_call rows store the raw reply blocks
and replay sends them verbatim (crash test: 4 requests for 4 turns across a
takeover, each an exact prefix-extension; mutant replay without raw fails it).
Thinking-mismatch 400 -> strip + retry once. Refusal/max_tokens = billed
Response: cost recorded, then error. Migration 008 tenants.run_mode: demo (fake,
$0, default) vs live (Haiku + real tools, $0.05/run). scripts/live_smoke.py is
the paid proof (refuses without key and --yes-spend). Demo run b9fcb695 OK.
New dep: anthropic. 25 new tests (173 total).

## P5 — Repair rung 3 + model fallback (built for $0)

Built by Claude Code; see learn/P5_ESCALATION.md. Explain-back owed.
Sonnet 5.5 only as a last resort: rung 3 after Haiku's answer AND its re-ask
fail (a repair_requested row with escalate_to; sticky, replay remembers it), or
a per-turn fallback when Haiku is still down after 3 retries (model_call records
fallback_from). Both only on a real provider and only if pricing.estimate()
(pessimistic: all prompt tokens uncached) fits the remaining budget, checked
BEFORE the call. check_run refined: the same reply after a re-ask isn't "paid
twice". 7 new tests (180 total).
