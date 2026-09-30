# Day 7 — Write tools and idempotency

*Built by Claude Code on 30 Sep 2026. Learning and explain-back still owed.*

## 1. The picture

Day 6 made a new worker re-run any tool call that has no logged result. That's
harmless for reading a web page. It's dangerous for **saving a company**: if the
first worker saved the row and died before writing `tool_result`, the second
worker would save it again.

**The payment QR analogy:** a shop gives every payment a receipt number *before*
it charges you. If the card machine freezes and you scan again, the machine looks
up the receipt number, sees "already paid", and prints the old receipt instead of
charging twice.

Relay does the same with a table called `tool_invocations`. Before a write runs,
its receipt number (the **idempotency key**) is written down. The write and
"receipt marked paid" commit together. A second attempt with the same key gets
the stored receipt back, and the write doesn't happen again. Underneath that,
`UNIQUE (tenant_id, domain)` on `companies` refuses a duplicate row even if
everything above failed.

## 2. Runtime story

### A normal run (`9546635e`)

The fake model now has 4 replies: search → fetch × 2 → **save** → final JSON.

| Turn | What happens | Ledger rows | Other tables |
|---|---|---|---|
| 1 | search | 1 `model_call`, 2–3 | — |
| 2 | fetch × 2 | 4 `model_call`, 5–8 | — |
| 3 | model asks for `save_company` | 9 `model_call`, 10 `tool_call` (with `idem_key`) | receipt `in_flight` → company row + receipt `ok` (one commit) |
| 3 | result back to the model | 11 `tool_result` | — |
| 4 | final JSON | 12 `model_call`, 13 `final` | — |

14 rows. Receipt: `attempts = 1, executions = 1`. One company row.

### Worker killed after the save committed (`bf7b65a7`, "kill B")

| Moment (DB clock) | What happens | Rows / tables |
|---|---|---|
| 11:58:41.8 | worker-2 writes the `save_company` `tool_call` | step 10 |
| 11:58:42.1 | receipt inserted, status `in_flight` | `tool_invocations` |
| 11:58:46.4 | company row **and** receipt `ok` commit together | `companies`, `tool_invocations` |
| — | `docker kill` worker-2, before `tool_result` | step 10 has no result |
| 11:58:58.1 | worker-1 claims, replay finds call_4 unconfirmed, writes a new `tool_call` | step 11 |
| same | `run_once`: the key's receipt is already `ok` → **returns the stored result, doesn't write** | `attempts` → 2, `executions` stays 1 |
| 11:59:02–05 | `tool_result`, last model call, `final` | steps 12–14 |

One company row, never updated (`created_at = updated_at`).

### Worker killed before the save committed (`fa4340ca`, "kill A")

| Moment | What happens |
|---|---|
| 12:00:26 | worker-2 writes `tool_call` (step 10) and the receipt (`in_flight`), then is killed before its write |
| 12:00:42 | worker-1 re-runs call_4: the receipt exists but is still `in_flight` → it does the write |
| 12:00:46.8 | company + receipt `ok` commit (worker-1's transaction) |

Again: `attempts = 2, executions = 1`, one company row.

## 3. Design decisions

| Choice | Alternative | Why |
|---|---|---|
| Key = `'<run_id>:<tool_call_id>'` | Blueprint: `sha256(run_id:step_index:tool:args)` | **Deviation.** Day 6 re-runs an unconfirmed call at a *new* step index; a key containing `step_index` would change and allow a second write. The model's call id is fixed inside the recorded reply |
| Key recorded in the `tool_call` payload (`idem_key`) | Only in `tool_invocations` | The ledger shows which receipt belongs to which call |
| The write and "receipt → `ok`" in **one transaction** | Write, then mark `ok` separately | With two commits, a crash between them leaves a written row and an `in_flight` receipt, and the retry writes again. With one, `in_flight` always means "the write did not commit" |
| The "mark ok" UPDATE is guarded: `WHERE status = 'in_flight'`; 0 rows → roll back our write, return the stored result | Trust that only one worker runs it | A stale worker racing the new owner (the Day 3 split-brain case) can't commit a second effect. Same idea as the guarded `mark_succeeded` |
| Extra column `executions` (not in the blueprint) | Only `attempts` | `attempts` counts how often a worker *asked*; `executions` counts how often the effect *committed*. The chaos harness needs the second number to report duplicates honestly |
| `save_company` writes under the run's domain (`ToolContext.domain`) and the run's tenant (sub-select on `runs`) | Use the domain the model wrote | The fake always says "Acme"; per-run domains give one row per run. It also stops a model from writing into another company's row |
| `ON CONFLICT (tenant_id, domain) DO UPDATE` | `DO NOTHING` | A second, separate run for the same domain should refresh the record. Idempotency is the key's job; the constraint is the last line of defence |
| Idempotency lives in the tool (`save_company` calls `run_once`); `dispatch_tool` stays generic | Wrap every tool in dispatch | Read tools don't need it (re-reading is harmless), and only a DB-writing tool can commit its effect in the same transaction as the receipt |
| `lookup_existing` reads by the domain the model gives, scoped to the run's tenant | Force the run's domain | Blueprint signature `(domain) -> Company`. Tenant scoping keeps it from reading other tenants |
| `flag_for_review` only returns a note | `approvals` table + `awaiting_approval` parking | **Out of scope for Stage 2**, as agreed. No `approvals` table, and the run isn't parked |
| No `idempotency_keys` table (client `Idempotency-Key` header) | Build it now | Not part of Stage 2. It's the blueprint's third layer; still a gap |
| `agent_version` → `enrich@v2` | Keep v1 | The prompt changed (it lists the new tools); the blueprint says bump the version on every prompt change |

## 4. Crash walk: inside `run_once` / `save_company`

| Worker dies right after… | DB state | Next worker |
|---|---|---|
| the `tool_call` row, before the receipt | no receipt | re-runs: inserts the receipt, writes once |
| the receipt insert (`in_flight`) | receipt, no company | re-runs: `attempts` 2, sees `in_flight`, writes once (**kill A**) |
| during the write, before commit | the transaction is rolled back by Postgres; receipt still `in_flight` | same as above |
| the commit (company + receipt `ok`) | company exists, receipt `ok` | re-runs: gets the stored result, **no write** (**kill B**) |
| `tool_result` | everything | replay confirms call_4; no re-run at all |

**Two workers at once** (a stale worker wakes up): both may start the write.
The second one's company upsert waits on the row lock, then its guarded UPDATE
finds the receipt already `ok`, so it rolls back. One effect commits.

**What this does NOT guarantee:** exactly-once for tools that talk to the
outside world. `web_search` and `fetch_page` may run twice, which is harmless
because they only read. The honest claim is the blueprint's: *at-most-once
dispatch of Relay's own writes; exactly-once end to end only for tools that
honour the key.*

## 5. The proofs

| Run | What was done | Predicted | Happened |
|---|---|---|---|
| smoke `9104c2dc` | `python -m scripts.idempotency_smoke`: same key twice, new key same domain, a left-over `in_flight` key | 8 checks | ✅ PASS (1 row; same key → stored result, `executions` 1) |
| `9546635e` | normal run through the API | 14 rows, 4 `model_call`, 1 company, receipt 1/1 | ✅ |
| **`bf7b65a7`** | kill **after** the save committed, before `tool_result` (temporary 4 s sleep after `run_once`) | stored result reused, `executions` 1 | ✅ company committed 11:58:46 by worker-2; worker-1's re-run at 11:58:58 wrote nothing; `attempts` 2, `executions` 1, 1 row, never updated |
| **`fa4340ca`** | kill **before** the write committed (temporary 4 s sleep before the INSERT) | the resuming worker writes once | ✅ company committed 12:00:46 by worker-1; `attempts` 2, `executions` 1, 1 row |
| `0bb540e3` | normal run after removing the temporary sleeps | as `9546635e` | ✅ |

Both kill runs: `scripts/check_run.py` OK (15 rows, 4 `model_call`, no gaps).
Both temporary sleeps were removed and never committed (`git checkout` of
`tools.py`, then a rebuild).

## 6. Questions for Yamini

1. Why is the key built from the tool call id and not from the step index?
2. The worker dies right after the receipt row is inserted as `in_flight`. Is it
   safe for the next worker to run the write? Why?
3. Why must the company INSERT and "receipt → `ok`" be in the same transaction?
   What goes wrong with two separate commits?
4. What does `WHERE status = 'in_flight'` on the "mark ok" UPDATE protect against?
5. If idempotency keys work, why keep `UNIQUE (tenant_id, domain)`?
6. `attempts` = 2 and `executions` = 1: what happened?
7. Why does `save_company` ignore the domain the model sends?
8. Is Relay "exactly-once"? What's the precise claim?

<details>
<summary>Answers</summary>

1. A re-run after a crash writes a new `tool_call` row at a new index (Day 6).
   The key must be the same for both attempts, and only the model's call id
   stays the same.
2. Yes. `in_flight` means the write's transaction never committed, because the
   write and the `ok` mark commit together. Nothing was saved, so saving now is
   the first and only time.
3. With two commits, a crash between them leaves a saved company and an
   `in_flight` receipt. The next worker would believe nothing happened and save
   again. One transaction means both happened or neither did.
4. A stale worker and the new owner running the same key at once. Only the first
   to mark `ok` keeps its write; the other finds 0 rows, rolls back its write and
   returns the stored result.
5. It's the last line of defence, and it covers what the key doesn't: two
   *different* runs for the same domain, or a bug that loses the key. The
   database itself refuses a second row.
6. Two workers tried the call (the first died), and the write committed once.
7. The row must belong to the run's own domain. The fake always says "Acme",
   and a real model could be tricked by a page into naming another company.
8. No. At-most-once dispatch for Relay's own writes (enforced by the database);
   exactly-once end to end only for tools that honour the key. Read-only
   external tools may run twice, which is harmless.

</details>

## 7. Interview lines

- "Every write tool call gets an idempotency key before it runs. The write and
  the key's 'done' mark commit in one transaction, so a retry either finds the
  stored result or knows nothing was written."
- "I killed a worker right after it saved a company but before it logged the
  result. The next worker replayed, re-issued the call, got the stored receipt
  back, and the company table still had exactly one row."
- "I don't claim exactly-once: it's at-most-once for our own writes, backed by a
  unique constraint as the last line of defence."
