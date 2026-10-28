# Day 8 — Retries, backoff, and the per-tool circuit breaker

## 1. The picture

Until now, one hiccup was one failure. If the fake fetch had been a real page
fetch and the site answered 503 once, the model was simply told "it failed" —
even though trying again two seconds later would have worked. And if a tool was
*completely* down, every worker would keep hammering it, run after run.

**The pharmacy analogy.** The nurse needs a medicine from the hospital pharmacy.
- **Retry with backoff:** the phone is busy. She doesn't redial instantly 50
  times; she waits a moment and tries again, up to 3 tries. The wait is a little
  random so all the nurses who got a busy tone don't redial at the same second.
- **Circuit breaker:** a sign at the ward desk: "pharmacy closed, flooded". The
  first nurse who finds out writes the sign; now nobody walks down for 30
  minutes. Then **one** nurse goes to check (the probe). Pharmacy open again →
  she removes the sign for everyone.

The sign is a row in Postgres, so all three workers share it — one worker
discovering an outage protects the other two.

```
 dispatch_tool("fetch_page", …)
        │
        ▼
 ┌─────────────────┐   open    ┌──────────────────────────────┐
 │ breaker.allow?  │──────────▶│ don't call; tell the model    │
 └────────┬────────┘           │ "circuit open, adapt"         │
          │ closed             └──────────────────────────────┘
          ▼
 ┌─────────────────┐  3 fails  ┌──────────────────────────────┐
 │ run the tool,   │──────────▶│ "failed after 3 attempts";    │
 │ retry on 503    │           │ each failure counted by the   │
 └────────┬────────┘           │ breaker (5 in a row → OPEN)   │
          │ success            └──────────────────────────────┘
          ▼
   result + breaker closed again
```

## 2. New words, one row each

| Term | Meaning | One-row example |
|---|---|---|
| `RetryableError` | "Try again, it may pass" | a 503, a timeout |
| `FatalError` | "It will fail identically again — don't retry" | a 400 bad request |
| `ToolError` (existed) | The model's own mistake; shown to it, never retried | fetching a URL that 404s |
| full-jitter backoff | sleep a RANDOM time in [0, ceiling); ceiling doubles per failure | after failure 1: 0–1.0 s |
| CLOSED / OPEN / HALF_OPEN | normal / skip this tool / one probe allowed | `fetch_page` open at 10:42:07 |

## 3. Each file's job

| File | One line |
|---|---|
| `relay/core/retry.py` (new) | Pure retry loop: retries only `RetryableError`, sleeps full-jitter backoff, max 3 tries |
| `relay/core/breaker.py` (new) | The shared sign: `allow` / `record_success` / `record_failure`, each one atomic SQL statement |
| `alembic/versions/…003…` (new) | The `circuit_breakers` table (one row per tool; no row = healthy) |
| `relay/agent/tools.py` | `dispatch_tool` now: breaker gate → run with retries → report attempts to the ledger |
| `relay/agent/loop.py` | Model calls retried the same way; still-down model → `error` step `model_unavailable` |
| `relay/llm/fake.py` | Can now fail on purpose (`failure_rate`), like a real provider under load |
| `chaos/fault_test.py` (new) | The proof: flaky-tools scenario and outage scenario, numbers to `chaos/fault_results.json` |

## 4. Runtime story: one fetch during a flaky patch

| Moment | What happens | Ledger rows written |
|---|---|---|
| model asks for `fetch_page` | loop writes intent | `tool_call` |
| attempt 1 | injected 503 → breaker counts failure #1 | — |
| sleep 0.4 s | random, under the 1.0 s ceiling | — |
| attempt 2 | page comes back fine → breaker resets to 0 | — |
| loop writes the receipt | `"attempts": 2` goes into the payload | `tool_result` |

The retries live INSIDE the step. The ledger gets one `tool_call` and one
`tool_result` either way — failed attempts never become steps, so a replayed
conversation never feeds the model its own noise.

And when `fetch_page` is fully down: dispatch 1 and 2 each burn 3 attempts
(6 failures — the breaker opens at 5). Dispatch 3 onwards doesn't even try:
the model is told *"fetch_page is temporarily unavailable (circuit open);
continue with the evidence you already have, or use flag_for_review"* and the
`tool_result` row carries `"breaker_open": true`.

## 5. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| Breaker state in Postgres | Redis (blueprint), or in-memory | Shared protection with zero new infrastructure; matches "the queue lives in Postgres". In-memory would make each worker learn the outage separately. |
| Retries inside the step, not as ledger rows | one row per attempt | The replayed conversation stays clean; `attempts` in the payload keeps the evidence. |
| Only `RetryableError` retried | retry every exception | A 400 or a code bug fails identically on attempt 2 and 3; retrying it just wastes the lease. |
| Healthy tool = no breaker row | insert a row per tool upfront | Healthy path costs one SELECT, zero writes. |
| Model has no breaker, only retries | breaker on the model too | There is exactly one model provider today; "open" would mean "stop working entirely". The blueprint's answer is a *fallback provider*, which needs real providers — deferred with them. |

## 6. Crash walk

| Worker dies… | What's in the DB | What the next worker does |
|---|---|---|
| mid-backoff sleep | `tool_call` without `tool_result` | Day 6 edge case 1: re-runs the tool (same idempotency key — Day 7 makes writes safe) |
| after `record_failure`, before the retry | failure count kept | Nothing special; the count is shared truth, the re-run continues it |
| while being the HALF_OPEN probe | `state = 'half_open'` | Nobody probes for `breaker_open_seconds`, then the expiry rule lets a new caller probe. The breaker can't jam. |
| after the model call, before `model_call` row | nothing for that call | Replay resumes; the fake re-serves the same reply (a real provider would re-charge — exactly one call's worth, the known Day 6 trade-off) |

## 7. The proofs (run in a cloud container on local PostgreSQL 16, not Neon)

- **Flaky scenario** (`--scenario flaky --n 50`, tool 503s 30% of the time,
  model 10%): **50/50 succeeded**, 48 of 200 tool calls needed retries,
  **41 recovered**, 7 exhausted all 3 attempts (the model was told and carried
  on), 12 model calls retried, **0 ledger problems**.
- **Outage scenario** (`fetch_page` down): breaker **opened after the 5th
  consecutive failure**, **13 calls were skipped** instead of hammering the dead
  tool, and after the outage ended it **closed itself via one probe**. Full
  numbers: `chaos/fault_results.json`.
- **Crash during retries:** run `28ca2373` — worker A killed (`kill -9`) after
  9 rows with 50% tool failures active; worker B reclaimed at attempt 2 and
  finished. 2 workers in `written_by`, no gaps, no repeated model calls.
- 21 new unit tests (`tests/test_retry.py`, `test_breaker.py`,
  `test_dispatch_retry.py` + a replay compatibility test); 57 pass in all.

## 8. Verify it yourself on Windows

**COMMANDS terminal** — apply the new migration and run the tests:
```powershell
alembic upgrade head
python -m pytest tests/ -q
```

**COMMANDS terminal** — make the fake internet flaky, then rebuild the workers:
```powershell
Add-Content .env "FAKE_TOOL_FAILURE_RATE=0.3"
docker compose up -d --build worker
```
(**LOGS terminal** — restart `docker compose logs -f worker` after the rebuild,
and check the worker ids changed.)

**API terminal** (if not already running):
```powershell
uvicorn relay.api.main:app --reload --port 8001
```

**COMMANDS terminal** — start a run and watch it (prediction first: with a 30%
failure rate, will the run fail? Answer below):
```powershell
curl.exe -s -X POST http://127.0.0.1:8001/v1/runs -H "Authorization: Bearer <your key from .env>" -H "Content-Type: application/json" -d "{\"domain\": \"flaky-test.example\"}"
python -m scripts.show_steps <run id from the reply>
```
Look for `"attempts": 2` or `3` inside `tool_result` payloads.

**COMMANDS terminal** — watch the breaker trip. Simulate the outage, rebuild,
start 3–4 runs, then look at the sign:
```powershell
Add-Content .env "FAKE_TOOL_DOWN=fetch_page"
docker compose up -d --build worker
python -m chaos.fault_test --scenario outage --note "Windows + Neon"
```
(`fault_test` spawns its own local workers from the venv, so it works even with
the Docker workers stopped — but stop them first so both sets don't race:
`docker compose stop worker`.)

**COMMANDS terminal** — clean up (remove the two lines from `.env`, rebuild):
```powershell
docker compose up -d --build worker
```

<details>
<summary>Prediction answer</summary>
The run still succeeds almost always. Each tool call gets 3 attempts, so at 30%
flakiness only ~2.7% of calls exhaust their retries — and even those come back
to the model as an honest "failed after 3 attempts" observation, not a crash.
</details>

## 9. Questions for you to answer later

1. Why does the sleep between retries need to be RANDOM, not fixed?
2. The breaker opens after 5 *consecutive* failures. Why consecutive, not
   "5 failures in total"?
3. A worker dies while it is the HALF_OPEN probe. Why doesn't the breaker stay
   stuck in half_open forever?
4. Why is a 404 (`ToolError`) not retried and not counted by the breaker?
5. Why do failed attempts not get their own ledger rows?
6. The worker dies right after `record_failure` but before sleeping. Is any
   state lost?
7. Why does the model get retries but no circuit breaker?

<details>
<summary>Answers</summary>

1. Three workers that fail together would retry together and hit the struggling
   service in waves. Random jitter spreads them out.
2. A tool that fails sometimes but works in between is flaky, not down — retries
   handle flaky. Only an unbroken streak says "down"; one success resets to 0.
3. `allow()` also lets a probe through when a HALF_OPEN row's `updated_at` is
   older than `breaker_open_seconds` — a dead probe simply expires.
4. The page genuinely doesn't exist; asking again changes nothing, and the TOOL
   is healthy — it answered correctly. Breaker counts tool outages, not model
   mistakes.
5. Replay turns ledger rows back into the conversation. Rows for failed
   attempts would feed the model its own failures and bloat every resumed run.
6. No. The failure count lives in Postgres, not in the worker. The reclaiming
   worker's re-run continues from the shared count.
7. A breaker needs somewhere else to send traffic (a fallback provider) or it
   just means "give up". With one provider, retry-then-fail-the-run is the
   honest behaviour; the fallback provider arrives with the real-model switch.
</details>

## 10. Interview lines

- "Transient failures are retried with full-jitter exponential backoff, but the
  retries happen *inside* a ledger step — failed attempts never pollute the
  replayed transcript, while the attempt count is still recorded for
  observability."
- "The per-tool circuit breaker is a Postgres row, not a Redis key: every
  statement is a single atomic UPDATE, all workers share the state, and the
  half-open probe is handed to exactly one worker by an UPDATE … WHERE state =
  'open' race that only one connection can win."
- "Under injected faults — 30% tool failures, 10% model failures — 50 of 50
  runs completed with zero ledger anomalies, and with a tool fully down the
  breaker opened after exactly 5 consecutive failures and recovered itself with
  a single probe."
