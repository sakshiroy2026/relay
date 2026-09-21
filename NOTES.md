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
