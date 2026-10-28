# P1 — The real model, behind the same loop

## 1. The picture

Everything so far ran on a scripted fake: four replies typed out in advance.
P1 plugs in the real thing, Claude Haiku 5.5, without touching the loop, the
ledger, replay, idempotency or the budget. They talk to an `LLMClient`, and
the new `AnthropicLLMClient` is just another one.

It was **built and tested for $0**. The tests drive the official SDK through a
mock HTTP transport: the SDK's real serializer, headers and error classes run,
and no request leaves the machine. The first paid run waits for Sakshi's key
and her "go".

The one thing that had to be designed rather than plugged in is **thinking**.
Haiku 5.5 thinks before it answers, and its reply carries `thinking` blocks
with a cryptographic `signature`. The API accepts a replayed thinking block
only if **everything before it is byte-identical** to when it was made. Relay
rebuilds every conversation from the ledger, on every turn and after every
crash, so the ledger must keep the model's reply blocks *exactly*.

**The notary analogy.** A notary stamps each page of a contract, and the stamp
covers everything written above it. Rewriting page 2 in your own words, even
saying the same thing, voids every stamp after it. So Relay photocopies each
page as it comes back (`raw_content`) and files the copy, never a retyped
version.

## 2. Each file's job

| File | One line |
|---|---|
| `relay/llm/anthropic_client.py` (new) | Relay conversation → Messages API request (tool messages grouped into one user turn; assistant turns replayed from `raw` verbatim) → Relay `Response` (text, tool calls, stop reason, uncached / cache-read / cache-write / output tokens, `raw_content`) |
| `relay/llm/client.py` | `Response` gains `cache_read_tokens`, `cache_write_tokens`, `raw_content`, `served_model`; `StopReason` gains `refusal` / `max_tokens` / `other` |
| `relay/llm/pricing.py` | real prices (Haiku 5.5 $0.10 / $0.50, long-prompt card above 100K; Sonnet 5.5 $2 / $10), cache reads 0.1×, cache writes 1.25×; `assert_priced` fails at boot, never after a paid call |
| `relay/llm/factory.py` | **run modes**: `demo` = fake model + fake tools, `live` = Haiku 5.5 + real tools; one SDK client per process; `preflight()` at worker start |
| `relay/core/replay.py` | `assistant_message()` (shared with the loop) carries `raw`; a billed refusal/max_tokens with no error row yet → `unhandled_stop` |
| `relay/agent/loop.py` | `run_agent(..., mode)`; the model is made *after* replay, so a misconfigured model writes `error: model_misconfigured` instead of crashing a worker; refusal / max_tokens: record the cost, then `error: model_refused` / `model_max_tokens` |
| `relay/agent/tools.py` | the run's mode picks the tool set (`registry_for`); real tools get their own breaker rows (`web_search@real`) so a fake success can never close a real outage |
| migration 008 | `tenants.run_mode` (`demo` default): paying is opt-in per tenant; the API copies it into `runs.input` |
| `relay/api/*`, `scripts/seed.py` | the Principal carries `run_mode`; live runs get `budget_usd = 0.05`; `seed.py --tenant NAME --mode live` |
| `scripts/live_smoke.py` (new) | the paid proof: refuses without the key and `--yes-spend`, prints the estimate first, stops at a total cap |

New dependency: `anthropic>=2.3,<3`, the official SDK (2.x sends API version
`2026-10-21`; Relay sends none of the parameters that version removes).

## 3. Runtime story: a live run that crashes in the middle

(This is exactly `test_every_request_extends_the_last_even_across_a_crash`.)

| Turn | Who | Request | Ledger |
|---|---|---|---|
| 0 | worker A | `[user]` → reply: thinking(sig-0) + web_search | `model_call` with `raw_content` [thinking sig-0, text, tool_use] |
| 1 | worker A | `[user, assistant(raw 0), tool_result]` → thinking(sig-1) + 2× fetch_page | `model_call` raw 1 + 4 tool rows |
| 2 | — | worker A is killed at the top of the turn | nothing |
| 2 | worker B | replays the ledger: **the same bytes** as A would have sent: `[user, raw 0, results, raw 1, results]` → thinking(sig-2) + save_company | |
| 3 | worker B | `[..., raw 2, result]` → thinking(sig-3) + final JSON | `final` |

4 requests for 4 turns (nothing paid twice), and every request's messages
start with the previous request's messages exactly. The signatures worker A
received are the ones worker B sent back.

## 4. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| Store the provider's reply blocks verbatim (`raw_content`) and replay them | Rebuild assistant turns from text + tool calls | A rebuilt turn is an edit, which voids every later thinking signature (400 on accounts created since 2026-08-31). Verified: our stored form equals what the SDK itself serializes, byte for byte |
| On "thinking block bound to a different conversation": strip thinking blocks, retry once | Fail the run | It only happens if the prompt or tool list changed mid-run (a deploy). The documented recovery loses that turn's reasoning, not the run |
| SDK `max_retries=0` | Let the SDK retry too | Day 8's loop retries with backoff and records `attempts` in the ledger. Two retry layers would multiply attempts and hide them |
| Refusal / max_tokens = a billed `Response`, not an exception | Raise | The call was paid for, so its cost must reach the ledger (the budget's truth) before the run fails. A crash between the two rows is handled by replay (`unhandled_stop`) without calling again |
| The model is created *after* replay, inside a guard | At the top of `run_agent` | A missing key or unpriced model ends the run with an `error` row and $0 spent; the worker keeps serving other runs |
| Per-tenant `run_mode`, default `demo` | One global `LLM_PROVIDER` | A public link can serve the free fake model to anyone while one private tenant runs the real model. Paying is opt-in, per tenant, never the default |
| Live runs get `budget_usd = 0.05` | The 0.25 default | ~8× a Haiku run's expected $0.006: plenty of room, tight enough that a runaway run is cheap |
| Prompt caching on (`cache_control` automatic) | Off | Each turn re-sends the whole conversation. Cached prefixes are read at 0.1× instead of full price. P3 measures the hit rate |
| `effort: medium` (Haiku 5.5's own default) | `low` | Start from the model's default; P3 measures `low` on real runs before changing it |
| No `thinking` parameter | Disable thinking | Adaptive thinking is the default and better for tool use; `effort` is the cost lever |

## 5. Crash walk

| Worker dies… | In the ledger | Next worker |
|---|---|---|
| after a `model_call` with thinking | the raw blocks | replays them verbatim: same request bytes, signatures valid |
| after a billed refusal, before its `error` row | `model_call` (stop `refusal`, cost) | replay → `unhandled_stop` → writes the error, no new call |
| mid-tool (real fetch) | `tool_call`, no result | Day 6: re-runs the read-only fetch |
| during an SDK call | nothing for that call | the call is made again (a real provider bills it again: exactly one call, the known Day 6 trade-off) |

## 6. The proofs (all $0)

- `tests/test_anthropic_client.py`, 21 tests through the real SDK and a mock transport:
  the request carries model / system / tools / `effort` / `cache_control`, no
  sampling parameters or thinking config, API version `2026-10-21`, and no key in the
  body; tokens split correctly; 429/529/500/408 → retry, 400/401/403/404/413 →
  fail; a connection error → retry; the SDK retries nothing itself (1 request per
  call); the thinking-mismatch 400 strips and retries exactly once; refusal and
  max_tokens come back billed; Haiku pricing incl. cache and the long-prompt card.
- **The crash test**: 4 requests for 4 turns across a kill and a takeover, each an
  exact prefix-extension of the last, signatures replayed. A mutant replay that
  drops `raw` fails it (run and reverted).
- A billed refusal is recorded (`cost_usd` > 0) then fails the run with
  `model_refused`; deleting that error row and resuming makes no new call.
- `tests/test_run_mode.py`: a demo tenant's run is `fake/fake` with the default
  budget; a live tenant's run is `anthropic/real` with $0.05; new tenants are `demo`.
- Live on uvicorn + one worker (local Postgres): demo run `b9fcb695` succeeded
  (14 steps, clean ledger); the worker's preflight warned that no keys were set.
- `scripts/live_smoke.py` refuses without `ANTHROPIC_API_KEY` and without `--yes-spend`.
- 173 tests pass.

**Not yet proven:** anything against the real API. Next session:
`python -m scripts.live_smoke --yes-spend --runs 3` (estimate ~$0.02, hard stop
$0.10), after Sakshi's go.

## 7. Questions for Sakshi Roy

1. Why can't replay rebuild an assistant turn from its text and tool calls any more?
2. Why is a refusal returned as a `Response` and not raised as an error?
3. Why are the SDK's own retries switched off?
4. Why is `run_mode` per tenant, and why is `demo` the default?
5. A deploy changes the system prompt while a live run is mid-flight. What happens
   on its next call?
6. Why is the model client created *after* the ledger is replayed?

<details>
<summary>Answers</summary>

1. A thinking block's signature covers everything before it. A rebuilt turn
   that says the same thing in different bytes is an edit, and the API rejects
   every later thinking block (400). Replay sends the stored blocks verbatim.
2. The provider already billed it. Raising would skip the `model_call` row, so
   the money would never reach the ledger the budget sums.
3. Relay's own retry loop (Day 8) already retries with backoff and records the
   attempt count in the ledger. SDK retries on top would multiply attempts
   invisibly.
4. A public demo should run free (fake model) for anyone, while only a chosen
   tenant runs the real model. Paying must be a deliberate switch, never the
   default for a new tenant.
5. The replayed thinking blocks no longer match (the system prompt is part of
   what they're bound to). The API answers 400; the adapter strips every
   thinking block and retries once; the run continues without that reasoning.
6. Replay may show the run is already finished or failed, so no model is needed
   at all. And a misconfigured model (no key, unpriced) can then be written
   down as an `error` step at the right index instead of crashing the worker.
</details>

## 8. Interview lines

- "The real model is an adapter behind the same `LLMClient` protocol; I tested it
  through the official SDK with a mock HTTP transport, so serialization, headers
  and error classes are real and the tests cost nothing."
- "Current Claude models bind each thinking block to the exact conversation before
  it, and Relay rebuilds conversations from a ledger. So the ledger stores the raw
  reply blocks and replay sends them verbatim. A test kills a worker mid-run and
  checks every request is a byte-exact extension of the previous one."
- "Paying is opt-in per tenant: the public demo runs a free scripted model, a live
  tenant runs Haiku 5.5 with a $0.05 per-run cap, and a refusal is recorded as a
  billed call before it fails the run, because the budget is computed from the
  ledger."
