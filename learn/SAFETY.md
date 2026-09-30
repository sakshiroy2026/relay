# Safety slice — API key check and per-run budget cap

## 1. The picture

Once Relay has a public URL and a real, paid model, two things can burn money:

- **A stranger** finds the URL and starts runs. Every run pays for model calls.
- **One runaway run** loops (a confused model, a bug) and keeps calling the model.

The safety slice closes both before the paid model or the URL exist:

- **Front-door lock:** every `/v1/...` request needs the API key.
- **Spending cap per run:** before each model call, the run checks what it has
  already spent. Once it reaches its budget ($0.25 by default), it stops.

**Analogy:** a prepaid metro card. The gate (API key) lets only card holders in,
and the card (budget) stops working once its balance is used up, however many
stations you meant to travel.

## 2. Runtime story

### A request without the key

| Step | What happens |
|---|---|
| `POST /v1/runs` with no `Authorization` header | FastAPI runs `require_api_key` before the route |
| the header is empty, so the check fails | `401 missing or invalid API key`; the route never runs; no row is written |

`/healthz` and `/readyz` sit outside `/v1`, so they stay open.

### A run with a tiny budget (run `e8280abe`, budget $0.0001)

| Turn | Check before the model call | What happens | Ledger rows |
|---|---|---|---|
| setup | replay: spent so far = $0 | — | row 0 `run_started` |
| 1 | $0 < $0.0001 → allowed | the model is called; it costs $0.004881 | 1 `model_call`, 2–3 search |
| 2 | $0.004881 ≥ $0.0001 → **stop** | no model call | 4 `budget_exceeded` |
| end | `run_agent` returns `None` | worker → `mark_failed` | status `failed` |

A normal run (`272f1414`) costs $0.026 in fake money (4 calls) and finishes well
under $0.25.

## 3. Design decisions

| Choice | Alternative | Why |
|---|---|---|
| One key from `.env` (`RELAY_API_KEY`), compared with `hmac.compare_digest` | Blueprint: bcrypt-hashed keys in `api_keys`, looked up by prefix, one per tenant | There is one user and one tenant. The full design is hardening, not safety; `api_keys` + `seed.py` already exist for later |
| `compare_digest`, not `==` | `==` | `==` stops at the first different character, so response times could leak how much of a guess was right. `compare_digest` takes the same time either way |
| The key is optional in `Settings`; if it's missing, `/v1` answers **503** | Run open when no key is set | A forgotten `.env` line must fail closed, never leave the door open |
| The check is a router-level dependency on `/v1` | A decorator on each route | New `/v1` routes on this router get it automatically (Stage 4's steps endpoint will) |
| Spent-so-far comes from the **ledger** (sum of `cost_usd` over `model_call` rows): replay sums it at claim time, then the loop adds each new call | Keep a counter on the `runs` row | The ledger is written before anything else, so it survives crashes. A resumed run keeps counting from the real total |
| Check **after the heartbeat, before the model call**; stop when spent ≥ budget | Check after the call | Before the call is the only point where money can still be saved. The price: one call can overshoot, because a call's cost is only known after it returns |
| Stop = a `budget_exceeded` step + return `None` → `failed` | A new status | `budget_exceeded` already exists in the `step_kind` enum (migration 001); `failed` + the ledger's last row explains why |
| Replay treats `budget_exceeded` like `error` (the run already failed) | Re-check the budget on resume | A resumed run must not start spending again |
| `runs.spent_usd` / `tokens_in` / `tokens_out` are refreshed from the ledger after every model call | Leave them at 0 | `GET /v1/runs/{id}` and the demo page can show cost. Only a display copy: the UPDATE recomputes the sums from `steps`, so a late or repeated refresh can't be wrong |
| Budget passed into `run_agent` from the claimed `runs` row | `run_agent` queries it | The worker already has the row from the claim; no extra query |

**Deferred on purpose** (still in the blueprint's Day 10): bcrypt + prefix
lookup, tenant scoping from the key, Redis rate limits, per-tenant concurrency
caps. With a paid model, add a spend limit at the provider as well; the budget
cap is per run, not per day.

## 4. Crash walk

| Worker dies right after… | DB state | Next worker |
|---|---|---|
| a `model_call` row | the cost is in the ledger | replay sums it; the budget check sees it |
| the budget check passes, before the model call | nothing new | replay: same spend, same check, calls the model |
| writing `budget_exceeded` | the reason is in the ledger, run still `running` | replay → `failed=True` → returns `None` → `mark_failed`; no model call |
| `refresh_run_totals` failed or never ran | the `runs` totals are stale | the next refresh after a model call writes the right sums; the budget itself never reads them |

## 5. The proofs

| What | Command | Predicted | Happened |
|---|---|---|---|
| no key | `curl.exe` POST `/v1/runs` without `Authorization` | 401 | ✅ 401 |
| wrong key | `Authorization: Bearer` + a made-up key | 401 | ✅ 401 |
| key without `Bearer` | `Authorization: <the key>` | 401 | ✅ 401 |
| GET without key | `GET /v1/runs/<id>` | 401 | ✅ 401 |
| health | `GET /healthz` | 200 | ✅ 200 |
| with the key | `Authorization: Bearer <the key>` | 202 | ✅ 202, run `272f1414` succeeded, 14 rows |
| budget | `python -m scripts.budget_test` | 1 `model_call`, then `budget_exceeded`, `failed` | ✅ PASS, run `e8280abe`: ledger `run_started, model_call, tool_call, tool_result, budget_exceeded`; `spent_usd` 0.004881 |
| display | `GET /v1/runs/272f1414…` with the key | `spent_usd` > 0 | ✅ `spent_usd` 0.026181, `tokens_in` 7007, `tokens_out` 344 |

Unit tests: `tests/test_replay.py` adds "spent = sum of `model_call` costs" and
"`budget_exceeded` present → failed" (12 tests pass).

## 6. Questions for Sakshi Roy

1. Why does the API answer 503, not 200, when `RELAY_API_KEY` isn't set?
2. Why `hmac.compare_digest` instead of `==`?
3. Why is the budget checked *before* the model call, and what's the cost of that
   choice?
4. Why compute spent-so-far from the ledger rather than from `runs.spent_usd`?
5. The worker dies right after writing `budget_exceeded`. What does the next
   worker do?
6. Why doesn't the budget cap alone protect you from a stranger with the URL?
7. Budget $0.0001: why is there still one `model_call` in the ledger?

<details>
<summary>Answers</summary>

1. A missing setting must fail closed. If an unset key meant "no check", one
   forgotten `.env` line on the server would open the API to everyone.
2. `==` returns as soon as a character differs, so its timing leaks how many
   leading characters of a guess were right. `compare_digest` takes the same
   time whatever the input.
3. It's the last moment the money can still be saved. The cost: the check can't
   know the next call's price, so a run can overshoot its budget by one call.
4. The ledger is written first and survives crashes; a resumed worker rebuilds
   the exact total with replay. `runs.spent_usd` is only a display copy that
   could lag behind.
5. Replay sees `budget_exceeded` and returns "already failed". `run_agent`
   returns `None` without calling the model, and the worker marks the run
   `failed`.
6. It caps each run, not the number of runs. A stranger could start thousands of
   cheap runs. The API key stops them at the door.
7. The check happens before each call, and before the first call nothing has
   been spent ($0 < $0.0001). The first call's cost is only known after it
   returns; the next check then stops the run.

</details>

## 7. Interview lines

- "Every `/v1` route needs a bearer key, compared in constant time. If the key
  isn't configured, the API refuses requests rather than running open."
- "Each run has a budget. Before every model call the loop compares the spend
  recorded in the ledger with the budget, so the cap survives crashes and
  replays. It can overshoot by at most one call."
- "I built the cheap safety pieces before the paid model and the public URL,
  because an open endpoint in front of a paid API is a bill anyone can run up."
