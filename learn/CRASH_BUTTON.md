# The "Kill the worker" button — the crash demo, self-service (29 Oct)

## 1. The picture

The README's whole pitch is "kill the worker, the run survives", but until now
only someone with `docker kill` could see it. Now the demo page has a red
button: any visitor can murder the worker that owns their run and watch
another one take over, live.

Think of a restaurant that claims its kitchen survives a cook walking out
mid-order. Before: you had to take the manager's word (screenshots of past
walkouts). Now there's a button on the table that sends the cook home
mid-dish, and you watch the next cook pick up the pan exactly where it was
left — because every completed step of the recipe is on the ticket, not in
the cook's head.

```
page ── POST /v1/runs/{id}/crash ──▶ API: demo tenant? run running?
                                      └─ UPDATE runs SET crash_requested_at = now()
worker (top of every agent turn, in beat()):
  crash_if_requested: clear the flag (commit) → os._exit(137)   ← no cleanup
  └─ lease stays held → expires (15 s on Render) → another worker claims
     → replays the ledger → resumes at the next step
```

## 2. Files

| File | What changed |
|---|---|
| `alembic/versions/c6f1a2d8e954_009_crash_requested.py` | `runs.crash_requested_at TIMESTAMPTZ` (additive) |
| `relay/api/repo.py` | `request_crash` (tenant-scoped, running runs only); `get_run` now also returns `lease_expires_at` + `server_now` for the page's countdown |
| `relay/api/routes/runs.py` | `POST /v1/runs/{id}/crash` — 403 for live tenants, 404/409 otherwise |
| `relay/worker.py` | `crash_if_requested` called in `beat()` before `extend_lease` |
| `relay/api/static/index.html` | full redesign: hero + measured numbers, example-domain chips, the kill button, lease countdown, takeover banner |
| `deploy/single/Dockerfile` | `LEASE_SECONDS=15` (nobody watches a 90 s pause), `FAKE_DELAY_SECONDS=2.5` (at 0.5 s a run was over in ~7 s — tested, too fast to click) |
| `tests/test_crash_button.py` | 7 tests |

## 3. Decisions

| Decision | Alternative | Why |
|---|---|---|
| Flag checked at the heartbeat, then `os._exit(137)` | kill the process from outside (no PID access across containers); raise an exception | `os._exit` skips atexit/finally — the closest thing to `docker kill` that the worker can do to itself. An exception would unwind politely, which is exactly not the demo |
| Clear the flag BEFORE dying (own transaction) | clear it on the next claim | the claim query is proven code (ground rule: don't touch it); and a flag cleared by its victim can never kill attempt 2 |
| Demo-mode tenants only (403 for live) | allow everywhere | a killed live run would re-do its last model turn after takeover — real money a visitor could waste |
| `WHERE lease_owner = me` on the clear | clear by run id alone | a worker that already lost the lease must not swallow a crash meant for the new owner (tested) |
| Countdown from `lease_expires_at` + `server_now` | client clock | visitors' clocks are wrong; the skew correction costs one column in a SELECT |

## 4. The crash walk (verified live, local run `484b68fe`)

1. Run started; worker `LAPTOP:23224` wrote steps 1–11 (search, two fetches, save).
2. Button pressed → 202, flag set.
3. At its next turn the worker cleared the flag and `os._exit(137)` — the
   page's countdown ran down its 15 s lease ("the run still belongs to a corpse").
4. Worker `LAPTOP:6368` claimed at attempt 2, replayed 11 steps, continued at 12.
5. Run `succeeded`: 14 steps, **4 model_call rows — the same as an uncrashed
   run** (`scripts/check_run` OK: no gaps, no duplicate model calls).

The flag is one-shot (cleared before death), so attempt 2 survives; a stale
worker can't clear the new owner's flag (`lease_owner = me`); a finished run
answers 409 ("nothing to kill").

## 5. What the page now is

One static HTML file, still: no framework, no build step, key in page memory,
ledger text rendered with `textContent` only. New: hero with the chaos-harness
numbers (labelled as measured, linked to the raw JSON), click-to-run example
domains, the kill button + countdown, and a three-card explainer (ledger /
lease + fencing / replay). SSE as before: the stream's 2 s fingerprint re-check
already covers takeovers (lease_owner is in the fingerprint), so the countdown
needed no new push path — only the two extra columns.

## 6. Questions

<details><summary>Why does the worker check only at the heartbeat, not mid-tool?</summary>
The heartbeat runs at the top of every agent turn, i.e. between ledger writes.
Dying there is enough for the demo and never leaves a half-written step. A
real `docker kill` can land mid-write — that case is what the 200-run chaos
harness and the idempotency receipts already prove.
</details>

<details><summary>Why not expose the crash to live tenants with their own consent?</summary>
The repair/takeover path is identical; the only difference is that the re-done
model turn costs real money. If a live-mode operator wants a chaos test, the
harness is the tool, not a public button.
</details>

<details><summary>What if two visitors watch the same run and both press kill?</summary>
The second press either lands while the flag is still set (idempotent — same
timestamp column) or after the first death, when the run has a new owner and
the button re-arms; a second kill then just demonstrates attempt 3. The
attempt ceiling (`max_attempts`) still bounds the run.
</details>

## 7. Interview lines

- "The landing page's demo button is the architecture: the API can't reach the
  worker, so 'kill yourself' is a row the worker polls — the same ledger-first
  thinking as everything else."
- "The kill is `os._exit(137)` after committing a one-shot flag clear: closest
  honest imitation of `docker kill` a process can do to itself, and the next
  attempt can't be re-killed by a stale flag."
- "After the kill the ledger has the same number of model calls as an
  uncrashed run — that's the whole product in one sentence."
