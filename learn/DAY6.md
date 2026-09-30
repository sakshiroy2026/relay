# Day 6 — Replay

## 1. The picture

Until Day 5, the conversation with the model (`messages`) lived only in the
worker's memory. When a worker died, the next worker started again from the
first user message. It asked the model the same questions again and paid for
them again.

**The nurse's chart:** a new nurse doesn't guess what happened on the last shift
and doesn't start the treatment again from the beginning. She reads the chart top
to bottom, sees "2pm dose given, 3pm dose prepared but not signed off", and
continues from there.

Replay is the new worker reading the chart. It rebuilds `messages` from the
`steps` table, in order, and carries on at the next free step number. A crash now
costs at most the one call that was in flight when the worker died.

## 2. Runtime story

### A normal run (nothing crashes)

`run_agent` first calls `replay_ledger(run_id)`. For a new run, the ledger has
only row 0 (`run_started`), so replay returns `[user message]`, next index 1 and
0 turns used. From then on the loop is the Day 5 loop.

| Turn | What happens | Ledger rows written |
|---|---|---|
| setup | replay reads row 0 → `[user]`, next index 1 | — |
| 1 | model asks for `web_search` → run it | 1 `model_call`, 2 `tool_call`, 3 `tool_result` |
| 2 | model asks for 2 × `fetch_page` → run both | 4 `model_call`, 5–6 call/result, 7–8 call/result |
| 3 | model returns the JSON record → valid | 9 `model_call`, 10 `final` |

11 rows, 0–10: the same as before Day 6 (run `beda9a11`).

### A crashed and resumed run (run `7b0200c8`)

| Moment | What happens | Ledger rows |
|---|---|---|
| worker-2, turn 1 | search asked for and run | 1 `model_call`, 2 `tool_call`, 3 `tool_result` |
| `docker kill` worker-2 | it dies before turn 2 | — |
| ~15 s | the lease runs out; nobody renews it | — |
| worker-3 claims (attempt 2) | replay reads rows 0–3 → `[user, assistant, tool]`, next index 4, **1 turn used** | — |
| worker-3, turn 2 | the fake counts 1 assistant turn → gives reply #2 (fetch × 2), **not** reply #1 again | 4 `model_call`, 5–8 |
| worker-3, turn 3 | final record | 9 `model_call`, 10 `final` |

Result: 3 `model_call` rows, the same as a run that never crashed. Nothing paid twice.

### A worker killed in the middle of a tool (run `efbc7f52`)

| Moment | Ledger rows |
|---|---|
| worker-2 writes the fetch reply and the first `tool_call` | 4 `model_call`, 5 `tool_call` (call_2) |
| `docker kill` before the page came back | row 5 has no `tool_result` |
| worker-1 claims; replay says "call_2 and call_3 were asked for but never confirmed" | — |
| worker-1 re-runs them **before** asking the model anything | 6 `tool_call` (call_2 again), 7 `tool_result`, 8 `tool_call` (call_3), 9 `tool_result` |
| turn 3 | 10 `model_call`, 11 `final` |

12 rows: one more than usual, because row 5 stays in the ledger as an honest
"started, not confirmed" marker.

## 3. Design decisions

| Choice | Alternative | Why |
|---|---|---|
| Split replay into a pure `rebuild_messages(rows)` and a one-query `replay_ledger(run_id)` | One function that queries and builds | The logic can be tested with hand-made rows and no database (`tests/test_replay.py`, 10 tests) |
| `relay.db` is imported *inside* `replay_ledger` | Import at the top like elsewhere | Importing `relay.db` opens the Neon pool. A top-level import would make the "pure" tests connect to Neon |
| Return a frozen dataclass `ReplayState` | A `(messages, index)` tuple | The edge cases need more than two values: turns used, pending tool calls, final, failed, unjudged answer |
| `run_started` → `user_message(domain)`, a function in `replay.py` | The loop builds the user message and replay copies the text | One place builds that sentence, so the replayed text can't drift from the original |
| Replay also replaces `next_step_index` | Keep calling `next_step_index` | Same query result: last `step_index` + 1. One read per claim is still "our belief" (the Day 3 rule) |
| `MAX_TURNS` counts `model_call` rows already in the ledger | A fresh 15 for every worker | Otherwise a run that crashes on every turn would never hit the cap |
| Unconfirmed tool calls are re-run before the next model call | Ask the model again | The model already asked; asking again costs money and could give a different answer |
| A re-run writes a **new** `tool_call` row | Reuse the old row | Rows are append-only, and the fencing rule forbids writing the same index twice |
| The loop's "run one tool" and "judge the final answer" became helpers (`_run_tool`, `_judge`) | Copy that code into the resume path | The resume path needs both. Copies would drift |

**One extra edge case, not in the plan:** the worker dies after writing the
final-looking `model_call` but before writing `final` / `error`. The ledger then
ends on an `end_turn` reply that nobody has judged. Asking the model again would
be wrong: it would pay again, and the fake would crash because it has no 4th
reply. So replay returns it as `unjudged_answer`, and the loop judges the stored
text without calling the model.

## 4. Crash walk

| Worker dies right after… | What's in the DB | What the next worker does |
|---|---|---|
| the claim, before any step | only row 0 | replay → `[user]`, starts at turn 1 as normal |
| a `model_call` with tool calls | the reply, no `tool_call` rows | runs every tool of that reply (all "pending"), then asks the model |
| a `tool_call` row | intent written, no result | writes a new `tool_call` for it, runs it, writes the result (run `efbc7f52`) |
| a `tool_result` | the result | uses it; next model call gets the next reply (run `7b0200c8`) |
| the last `model_call` (`end_turn`) | the answer, not judged | judges the stored answer; no model call |
| `final` | the record | returns it straight away; the worker calls `mark_succeeded` |
| an `error` row | the reason | returns `None`; the worker calls `mark_failed` |

**What's still unsafe:** re-running a tool is fine today because `web_search` and
`fetch_page` only read. A write tool (`save_company`, Day 7) would run twice.
Day 7's idempotency key closes that gap.

## 5. The proofs

Setup: 3 worker containers, `LEASE_SECONDS=15`, `FAKE_DELAY_SECONDS=5` during the
crash tests (removed afterwards). The kills were run by a small watcher script
that POSTed a run, polled the ledger, and ran `docker kill` on the container
whose id matches `lease_owner`.

| Run | What was done | Predicted | Happened |
|---|---|---|---|
| `96bc14ef` | normal run (a worker on the host, before Docker was up) | 11 rows 0–10 | ✅ 11 rows, `succeeded`, attempt 1 |
| `5f582bda`, `2935b996`, `beda9a11` | normal runs in the containers | 11 rows each, 1 writer | ✅ |
| **`7b0200c8`** | `docker kill` the owner once rows 0–3 existed | attempt 2, next worker writes step 4 = reply #2, 3 `model_call`, 11 rows | ✅ exactly that: worker-2 `d577e6e8` wrote 1–3, worker-3 `3ba4f043` wrote 4–10 |
| **`efbc7f52`** | `docker kill` while a `fetch_page` was running (temporary 3 s sleep in the fake fetch, **not committed**) | row 5 `tool_call` without a result, then a new `tool_call` for call_2 | ✅ rows 5 and 6 both `tool_call` call_2, result at 7, 3 `model_call`, 12 rows, no gaps |
| `6df488ea` | first attempt at the kill above: aimed at "4 rows" | — | the kill landed later, after row 5 (a dangling `fetch_page` `tool_call`). It resumed correctly anyway: 12 rows, 3 `model_call`. The watcher's ~6 s lag wasn't explained; with timestamps added, the next attempt killed within 1 s |

`python -m scripts.check_run <run_id>` passes ("no gaps, no duplicate
model_call") on all of these runs.

**Not exercised live, unit tests only:** "`final` already written", "`error`
already written" and "unjudged answer". Those crash windows are a single
database round trip wide, and hitting them by hand isn't realistic. The chaos
harness (Stage 5) may land in them by chance.

**Note on timestamps:** the Docker VM's clock ran about 15 s behind Neon's clock
during these tests. `docker compose logs` times and `steps.created_at` times
don't line up. The lease uses only the database's `now()`, so this doesn't
affect correctness.

## 6. Questions for Sakshi Roy

1. Why does a `tool_call` row never become a message?
2. The worker dies right after writing row 4 (`model_call` asking for two
   fetches). What does the next worker do first, and does it call the model?
3. Why does a re-run tool get a **new** `tool_call` row instead of reusing the
   old one?
4. Why must the fake model count assistant turns instead of keeping a counter,
   now that replay exists?
5. Why does `MAX_TURNS` start counting from the ledger instead of from 0?
6. The worker dies after row 9 (the final `model_call`) but before row 10
   (`final`). What happens, and what would go wrong without `unjudged_answer`?
7. Why is re-running tools safe today but not after Day 7?

<details>
<summary>Answers</summary>

1. It's a receipt of intent, like writing "about to give 3pm dose" before giving
   it. The model never saw it; it only sees the call inside the assistant message
   and then the tool's result.
2. Replay sees that call_2 and call_3 have no `tool_result`, so it runs both
   (rows 5–8) **before** any model call. The model isn't called for that turn:
   the reply is already in the ledger.
3. The ledger is append-only and `UNIQUE(run_id, step_index)` forbids writing an
   index twice. The old row stays as evidence that a first attempt started.
4. A counter would be 0 in the new worker's memory, and it would give reply #1
   again. Counting assistant turns in the replayed `messages` gives the next
   reply, exactly as a real model continues a conversation it is shown.
5. Otherwise a run that crashes on every turn would get a fresh 15 turns each
   time and could loop forever. The cap is per run, not per worker.
6. The answer is judged from the stored text: `final` (or `error`) is written at
   index 10, with no model call. Without it the loop would ask the model a 4th
   time: it would pay twice for the answer, and the fake would raise
   `RuntimeError` because its script has only 3 replies.
7. `web_search` and `fetch_page` only read, so doing them twice changes nothing.
   `save_company` writes a row; doing it twice could create a duplicate. Day 7
   adds an idempotency key checked before executing.

</details>

## 7. Interview lines

- "On reclaim, a worker rebuilds the agent's conversation from the ledger with
  one ordered SELECT. It replays the transcript, not the code, so a crash costs
  at most the one model call that was in flight."
- "I killed a worker mid-run with `docker kill`. Another worker resumed at the
  next step, and the run still had exactly three model calls: nothing paid twice."
- "Tool calls are logged before they execute. On resume, any call without a
  logged result is re-run. That's safe for read-only tools, and idempotency keys
  make it safe for writes."
