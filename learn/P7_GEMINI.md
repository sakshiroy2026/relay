# P7 — A real model Relay can actually afford, and a public toggle for it

## 1. The picture

P1 built the real-model adapter and then stopped: the Anthropic key never
arrived (a card problem). So the live site has been running a scripted fake
model since it went up — honest, but a visitor can't tell a durable runtime
from a slideshow if nothing is actually thinking.

P7 plugs in **Google Gemini on the free tier**. No card, no billing, no paid
plan. The loop, the ledger, replay, idempotency and the budget don't change at
all: they talk to an `LLMClient`, and `GeminiLLMClient` is just another one,
sitting next to `AnthropicLLMClient` (which stays, untouched, for the day a
Claude key exists).

Then **Option A**: the public page gets a toggle, *"Run with real AI"*. All
visitors share **30 real-AI runs a day**, counted in Postgres. Past that the run
quietly uses the demo model and says so. A visitor's real-AI run gets the real
model but the **fake tools** — canned search results and canned pages — so a
stranger can never make this server fetch a URL of their choosing.

The one thing that had to be designed rather than plugged in is **thought
signatures**. Gemini thinks before it answers, and the parts of its reply carry
a `thoughtSignature`. Those signatures are only valid inside the conversation
that produced them and must be replayed unchanged. Relay rebuilds every
conversation from the ledger, on every turn and after every crash — so the
ledger has to keep the model's reply parts *exactly*.

**The notary analogy, again.** This is P1's problem in a different alphabet. A
notary stamps each page, and the stamp covers everything written above it.
Retyping page 2 in your own words — same meaning, different bytes — voids every
stamp after it. So Relay photocopies each page as it comes back
(`raw_content`) and files the copy, never a retyped version. Anthropic calls the
stamp a thinking `signature`; Google calls it a `thoughtSignature`. The
discipline is identical, which is the point: the ledger already had the right
shape.

## 2. Each file's job

| File | One line |
|---|---|
| `relay/llm/gemini_client.py` (new) | Relay conversation → `generateContent` request (tool results grouped into one user turn; model turns replayed from `raw` verbatim) → Relay `Response` (answer text with thought parts excluded, tool calls, stop reason, uncached / cache-read / output tokens, `raw_content`) |
| `relay/llm/pricing.py` | Gemini prices at Google's **paid** rates (3.5 Flash-Lite $0.30 / $2.50, 3.1 Flash-Lite $0.25 / $1.50, 3.8 Flash $0.75 / $3.75) so the budget cap and `/ops` keep meaning something — the free tier bills $0 |
| `relay/llm/factory.py` | `public_real` mode = Gemini + **fake** tools; `live` = Gemini + real tools; one client per provider per process; `escalation_allowed()` says only Anthropic may escalate; `preflight()` warns on a missing `GEMINI_API_KEY` |
| `relay/agent/loop.py` | asks `escalation_allowed(provider)` instead of comparing to `"anthropic"`, so no Claude model sits on a path a Gemini run could take |
| `relay/config.py` | `GEMINI_MODEL`, `GEMINI_MAX_OUTPUT_TOKENS`, `PUBLIC_REAL_RUNS_PER_DAY`; `llm_provider` gains `gemini`; `LIVE_RUN_BUDGET_USD` 0.05 → 0.25 (Gemini's paid rates are higher than Haiku's) |
| migration 010 | one **partial index** on `runs (tenant_id, created_at) WHERE input->>'mode' = 'public_real'` — the day's count is a count of rows, so there is no new table to keep in sync |
| `relay/api/repo.py` | `real_runs_left()`; `create_run(..., real_ai=)` resolves the mode **inside the existing per-tenant advisory lock**; `RealAIBusy` when one real-AI run is already in flight |
| `relay/api/routes/runs.py` | `POST /v1/runs` takes `real_ai`; answers with the mode it actually gave, the allowance left, and a plain-English `note`; 409 for a non-demo tenant, 429 + `Retry-After` while the one slot is busy |
| `relay/api/main.py` | `/demo-config` also reports `real_ai`, the model id, and how many runs are left today |
| `relay/api/static/index.html` | the toggle, "N real-AI runs left today", a `demo model` / `real AI` pill on the run, and an honest "waiting for the one real-AI slot" state |
| `scripts/live_smoke.py` | now provider-aware: prices from the model the `live` mode actually uses, checks that provider's key, and says out loud when a free tier is in use |

**No new dependency.** The adapter speaks the REST API over `httpx2`, which the
Anthropic SDK already pulls into the image. It is now declared in
`pyproject.toml` anyway: relying on another package's dependency is not a
dependency.

## 3. Which model, and why

`GET /v1beta/models` with the key (free, no generation quota) lists 60 models.
The pick is a setting, `GEMINI_MODEL`, defaulting to **`gemini-3.5-flash-lite`**:

- stable, not a preview (previews get tighter free limits);
- `generateContent` + function calling + thinking, 1M context;
- on the free tier per Google's pricing page (29 Oct 2026);
- Flash-**Lite** rather than Flash: the cheaper, higher-throughput tier, which
  matters because a run is 6–10 model calls and the free tier's limit is
  per-minute as well as per-day.

Two other free-tier models are priced in `PRICES` so switching is one env var:
`gemini-3.1-flash-lite` (cheaper) and `gemini-3.8-flash` (newest, strongest,
but the Flash tier's free allowance is the one Google no longer publishes
per-model — it is only visible in AI Studio under *View your active rate
limits*).

## 4. Runtime story: a real-AI run that crashes in the middle

(This is exactly `test_every_request_extends_the_last_even_across_a_crash` in
`tests/test_gemini_client.py`.)

| Turn | Who | Request | Ledger |
|---|---|---|---|
| 0 | worker A | `[user]` → reply: thought(sig-0) + web_search | `model_call` with `raw_content` [thought sig-0, text, functionCall sig-0-web_search] |
| 1 | worker A | `[user, model(raw 0), functionResponse]` → thought(sig-1) + 2× fetch_page | `model_call` raw 1 + 4 tool rows |
| 2 | — | worker A is killed at the top of the turn | nothing |
| 2 | worker B | replays the ledger: **the same bytes** A would have sent: `[user, raw 0, responses, raw 1, responses]` → thought(sig-2) + save_company | |
| 3 | worker B | `[..., raw 2, response]` → thought(sig-3) + final JSON | `final` |

4 requests for 4 turns (nothing asked twice), every request's `contents` start
with the previous request's `contents` exactly, and the signatures worker A
received are the ones worker B sent back.

## 5. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| Raw `httpx2` on the REST endpoint | the official `google-genai` SDK | Render's free plan is 512 MB and ~0.1 CPU, and the image already settles at 120 MB. The adapter is one POST and one JSON walk; a second SDK would add install weight, import time and a second retry layer to switch off. The Anthropic adapter uses its SDK because that SDK's serializer is what defines a valid replayed thinking block — Gemini's parts are plain JSON we already store |
| Store the reply's parts verbatim and replay them | rebuild model turns from text + calls | A rebuilt turn is an edit, which voids the thought signatures after it. Same reasoning as P1 |
| `parametersJsonSchema`, titles stripped | clean the schemas into the OpenAPI 3.0 subset `parameters` takes | Pydantic emits `anyOf: [T, null]` for Optionals, `additionalProperties` for `dict[str, float]` and `format: uri` for `HttpUrl`. The OpenAPI subset drops those, so the model would be free to send args the Pydantic models then reject — a silent quality loss, not an error. Titles go because they are tokens we pay for |
| A daily-quota 429 is **fatal**, a per-minute 429 is retryable | one rule for all 429s | Backoff fixes a per-minute quota. The per-day quota returns at midnight Pacific; retrying it three times just burns the run's attempts and still fails. The quota id in the error body says which it is |
| A `functionCall` with no `id` gets a **deterministic** stand-in (`relayfc-<turn>-<pos>`) | a random id | The id becomes the tool's idempotency key, `<run_id>:<id>`. A random one would make a resumed run re-execute a write tool under a new key. The prefix is also how the adapter knows not to echo the id back to Google |
| No `thinkingConfig` sent | set a thinking budget / level | The parameter is spelled differently across model generations. Nothing sent = each model's own default, and `GEMINI_MAX_OUTPUT_TOKENS` (16000) leaves room for the thinking that is billed as output |
| Priced at Google's **paid** rates | price free-tier calls at $0 | A $0 price makes the budget cap and every `/ops` spend number meaningless, and moving to a paid key later would silently remove the guard rail. The README says the real bill is $0 |
| Gemini runs never escalate | point `ESCALATION_MODEL` at a Gemini model | Rung 3 exists to try a *stronger* model after the cheap one's chances are spent. The Claude ids a Gemini client cannot call are now gated by `escalation_allowed()`, so nothing is left on a path that would fail the moment it was taken. Rungs 0–2 of the repair ladder still run |
| Public real-AI runs get **fake** tools | real tools | Real tools mean `fetch_page`. Letting a stranger choose the URL this server fetches is an SSRF surface the guard shouldn't have to be perfect about. The model is real; the internet is canned, and the page says so |
| The cap is a count of `runs` rows | a counters table | One partial index, nothing to keep in sync, and the count is already inside `create_run`'s per-tenant advisory lock — so two visitors racing for the last run of the day cannot both get it |
| Over the cap → a demo run, not an error | 429 | A visitor who clicked the toggle still gets a working demo of the thing the page is actually about. `mode` and `note` tell them which model ran |
| One real-AI run at a time, 429 + `Retry-After` while busy | let them run concurrently | The free tier allows roughly ten requests a minute per project and one run is 6–10 of them; two at once would simply 429 each other mid-run. Nothing is created and no allowance is spent, so the page can wait and ask again |
| A real-AI run stuck over 5 minutes stops holding the slot | hold it until it finishes | One abandoned run would otherwise close the real-AI demo for the rest of the day |
| The day resets at midnight **US Pacific** | UTC, or IST | That is when Google's own per-day quota resets. When Relay's cap reopens, the quota behind it has too |
| `real_ai` joins the idempotency body hash only when true | always include it | Keys minted before P7 keep hashing to exactly what they hashed then, so a retry across the deploy still replays instead of 422-ing |

## 6. Crash walk

A visitor starts a real-AI run and hits **Kill the worker** at step 3.

1. The API arms `crash_requested_at` (demo-mode tenants only — a `public_real`
   run belongs to the demo tenant, so the button still works).
2. The owning worker sees the flag at its next heartbeat, clears it and exits
   with no cleanup. The lease is left to expire.
3. Another worker claims the run and replays the ledger: the user message, each
   model turn's parts **verbatim including every `thoughtSignature`**, each
   tool result under the id Gemini gave (or the deterministic one Relay minted).
4. Its first request to Google is byte-identical to what the dead worker would
   have sent, so the replayed signatures are accepted. Nothing already asked is
   asked again; `save_company` re-issued after the kill still writes once.
5. The run finishes. On the free tier the whole episode costs $0 and a handful
   of the day's free requests.

## 7. The proofs

**All $0 and no network: 43 tests in `tests/test_gemini_client.py`** drive the
adapter against `httpx2.MockTransport`.

| What | Test |
|---|---|
| the key goes in `x-goog-api-key`, never the URL | `test_the_key_travels_in_a_header_never_in_the_url` |
| roles, grouped tool results, empty results | `test_roles_and_tool_results_become_gemini_contents` |
| a Relay-minted id is not echoed to Google | `test_a_relay_minted_call_id_is_not_echoed_back_to_gemini` |
| **every real tool schema** converts, Optionals / `additionalProperties` / `format` intact | `test_every_real_tool_schema_converts` |
| a tool-call turn, with thought parts kept but not read as the answer | `test_a_tool_call_turn_comes_back_as_tool_use_with_its_parts_kept` |
| a missing call id is deterministic across replays | `test_a_call_without_an_id_gets_a_deterministic_one` |
| all eight stop reasons; a `functionCall` beats the finish reason; a blocked prompt is a refusal | `test_stop_reasons_map_to_relays_five` + 2 |
| per-minute 429 retryable **vs** daily-quota 429 fatal | `test_a_per_minute_429_is_retryable`, `test_a_daily_quota_429_is_fatal_so_it_cannot_burn_every_retry` |
| an error never quotes a header | `test_an_error_never_quotes_a_header` |
| a refused signature is stripped and retried once | `test_a_refused_signature_is_stripped_and_retried_once` |
| **verbatim signature replay across a kill and a takeover** | `test_every_request_extends_the_last_even_across_a_crash` |
| a metered refusal is recorded, then fails the run | `test_a_billed_refusal_is_recorded_then_fails_the_run` |
| no key → `model_misconfigured`, worker lives | `test_live_mode_without_a_key_fails_the_run_not_the_worker` |
| no Claude model on a Gemini path | `test_gemini_runs_never_escalate_to_a_claude_model` |
| **run 31 quietly gets the demo model** | `test_run_31_quietly_gets_the_demo_model` |
| the cap is a query, not process state | `test_the_cap_is_counted_per_day_not_per_process` |
| one real-AI run at a time | `test_one_real_ai_run_at_a_time` |
| **a non-demo tenant cannot use the opt-in** | `test_a_live_tenant_cannot_use_the_public_opt_in` |
| pre-P7 idempotency hashes unchanged | `test_the_opt_in_does_not_change_pre_p7_idempotency_hashes` |

251 tests pass in total (208 before P7). `ruff format`, `ruff check` and
`mypy` are clean.

**The one real check:** see §9. Nothing in this document claims a real Gemini
call until that section names run ids.

## 8. Questions for Sakshi Roy

1. A visitor's real-AI run gets the real model but canned search results. Is
   that still an honest demo, or does the page need to say it louder?
2. The ledger prices a free-tier call at Google's paid rate. Who is that number
   for — and what would it mean if the page showed `$0.00` instead?
3. Two visitors click "real AI" in the same second. Why can only one of them get
   the last run of the day, and which line of SQL decides it?
4. A worker dies mid-run on Gemini. Why can't the next worker just re-describe
   the earlier turns in its own words?

<details>
<summary>Answers</summary>

1. It's honest because the *runtime* is what the page is about: a real model
   really is making the decisions, really is being crashed, and really is being
   resumed from the ledger. What's canned is the internet it reads. The page
   says so in the toggle's own text, and the alternative — letting a stranger
   name a URL this server will fetch — hands out an SSRF primitive on a free
   container.
2. It's for the budget cap and the `/ops` spend numbers: a per-run cap that
   counts $0 per call is not a cap at all, and the day a key becomes a paid key
   nothing would warn you. `$0.00` on the page would be *truer about the bill*
   and *useless as a guard rail*, so the README states both: priced at the paid
   rate, billed at zero.
3. Only one can, because the count of today's `public_real` runs happens inside
   the same `pg_advisory_xact_lock(hashtextextended(tenant_id, 42))` that the
   insert does. The second request blocks on that lock for a few milliseconds,
   then counts again — and sees the first one's committed row. The deciding
   line is the `SELECT pg_advisory_xact_lock(...)` that was already there for
   the concurrency cap; P7 just does its counting inside it.
4. Because Gemini signs its reasoning. Each part of a thinking turn carries a
   `thoughtSignature` that is only valid if everything before it is unchanged.
   A re-described turn is new bytes, so every signature after it stops
   verifying and the request is rejected. The ledger therefore stores the
   provider's parts verbatim in `raw_content`, and replay sends those exact
   parts back — the same discipline P1 needed for Claude's thinking blocks.

</details>

## 9. What was actually verified

Filled in from the real check, with run ids. Until then: built and tested for
$0, nothing real has run.

## 10. Interview lines

- "The adapter is 300 lines and the loop didn't change, because durability
  lives in the ledger, not in the provider. Swapping Claude for Gemini meant
  writing one `LLMClient` and one price row."
- "Both providers sign their reasoning, and a signature covers every byte
  before it. That's why a crash-safe agent has to store the provider's reply
  verbatim — the thing that makes replay *valid*, not just *possible*."
- "A per-minute rate limit and a per-day quota look identical on the wire — both
  are 429 `RESOURCE_EXHAUSTED`. One is what retries are for; the other will
  still be there after every retry. Telling them apart is the difference
  between a run that fails in 3 seconds and one that fails in 30."
- "The public real-AI cap is a `count(*)` inside the advisory lock the
  concurrency cap already took. No counters table, no Redis, no drift — and
  two visitors racing for the last run of the day get one run, not two."
- "A free model for strangers is a safety design, not a cost design: they get
  the real model and a canned internet, because the one thing I won't hand a
  stranger is a URL this server will fetch."
