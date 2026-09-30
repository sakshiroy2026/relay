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

Built by Claude Code; see learn/DAY6.md. Explain-back owed.
Proofs: kill mid-run `7b0200c8` (resumed at step 4, 3 `model_call`, 11 rows),
kill mid-`fetch_page` `efbc7f52` (new `tool_call` row for the re-run, 12 rows).

## Day 7 — Write tools + idempotency

Built by Claude Code; see learn/DAY7.md. Explain-back owed.
Proofs: kill after `save_company` committed `bf7b65a7` and before it committed `fa4340ca`:
1 company row each, receipt `attempts = 2, executions = 1`.
