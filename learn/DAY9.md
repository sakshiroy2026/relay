# Day 9 — The schema repair ladder and the dead-letter queue

## 1. The picture

Two kinds of failure were still handled badly. First: the model's final answer
is *almost* right — the correct record wrapped in a code fence, or with
`founded_year: 1599` — and Relay threw the whole run away over it. Second: a run
that failed for an outside reason (the model was down, the worker kept
crashing) just lay there as `failed`, or worse, invisible: a run whose five
attempts were spent was skipped by the claim query forever, with nothing
marking it.

**The pharmacy, again.** A prescription comes back with the dose written in the
margin instead of the box. You don't throw it away: first you just read it
properly (free), and if it's genuinely wrong you send it back once with a note
saying exactly what's wrong (costs one trip). That's the **repair ladder**.
And prescriptions that can't be filled today go into a labelled tray — not the
bin — so the morning shift can look at the tray, fix the cause, and put them
back in the queue. That's the **dead-letter queue**.

## 2. New words, one row each

| Term | Meaning |
|---|---|
| repair ladder | Try the cheapest fix first: parse strictly (rung 0) → extract the JSON out of the noise, free (rung 1) → tell the model its validation errors and ask once for corrected JSON (rung 2, one paid call) |
| `repair_requested` (new step kind) | The rung-2 correction message, written to the ledger so replay rebuilds the conversation word for word |
| dead-letter queue (DLQ) | Where runs that can't proceed are parked *visibly*: status `dead` (in the enum since Day 1, never used until now) plus the reason on the row |
| the reaper | Idle workers sweep runs whose lease expired with all attempts spent → `dead`, reason `max_attempts_exhausted` |
| `requeued` (new step kind) | An operator put a failed/dead run back in the queue. Replay treats the verdict before it as cleared — the run **resumes from its ledger**, it does not restart |
| rung 3 (not built) | Escalate to a *stronger* model. Needs a second real model; deferred with the real-model switch |

## 3. Each file's job

| File | One line |
|---|---|
| `relay/core/repair.py` (new) | Pure rungs 0–1: strict parse, then balanced-brace JSON extraction; builds the rung-2 correction message |
| `relay/agent/loop.py` | `_judge` is now the ladder: final → `final` row (tagged `repaired_by` if salvaged); broken + re-ask left → `repair_requested` row and the loop's next model call is the re-ask; out of rungs → `error` |
| `relay/core/replay.py` | `repair_requested` → the user message + a used-up re-ask; `requeued` → clears a preceding `error`/`budget_exceeded`; `final` strips the `repaired_by` tag out of the result |
| `relay/core/dlq.py` (new) | `reap_exhausted` (expired lease + attempts spent → `dead`) and `requeue_run` (marker step first, then flip to `pending` with `max_attempts = attempts + 5`) |
| `relay/worker.py` | Idle moments run the reaper (one indexed UPDATE, usually matches nothing) |
| `scripts/dlq.py` (new) | The operator's chair: `list` (grouped by reason), `show`, `requeue` (by id or `--reason`, optional `--budget`) |
| migration 004 | The two step kinds. No new tables: the DLQ is the runs table, the requeue is a ledger row |
| `relay/agent/fake_script.py`, `config.py` | `FAKE_BROKEN_FINAL` = fenced / prose / bad_year / hopeless breaks the fake's final answer for the proofs, with one corrected reply appended for the re-ask |
| `chaos/repair_dlq_test.py` (new) | The numbers: repair success rate per breakage, the outage→requeue→resume round trip, the kill-until-exhausted→reaper→requeue drill |

## 4. Runtime story: one broken final answer (rung 2)

`FAKE_BROKEN_FINAL=bad_year`: the model's final record has `founded_year: 1599`
(valid JSON — extraction can't help; the *value* is wrong).

| Moment | What happens | Ledger rows |
|---|---|---|
| turns 1–3 | search, fetch ×2, save — as always | 1–11 |
| turn 4 | the model replies `end_turn` with the bad record | 12 `model_call` |
| judge | rung 0 fails (`founded_year ≥ 1600`), rung 1 extracts nothing better | — |
| rung 2 | the validation errors go back to the model: *"Reply with the corrected JSON record only"* | 13 `repair_requested` |
| turn 5 | the corrected record comes back and validates | 14 `model_call`, 15 `final` (tagged `repaired_by: ["model_reask"]`) |

The run costs one extra model call and succeeds. A fenced/prose answer never
reaches rung 2: extraction fixes it for free, and the `final` row is tagged
`repaired_by: ["extracted_json"]`. A hopeless answer (no JSON, and the re-ask
no better) ends in `error: invalid_final_answer` after exactly one re-ask —
the ladder never loops.

**And the DLQ round trip**, for a run that failed `model_unavailable` at turn 2:

| Moment | What happens | Ledger |
|---|---|---|
| outage | turn 2's model call fails all 3 retries | rows 0–8, then 9 `error (model_unavailable)`; run `failed` |
| operator | `python -m scripts.dlq requeue --reason model_unavailable` | 10 `requeued`; run `pending`, `max_attempts = attempts + 5` |
| recovery | a worker claims it, replays rows 0–10 | the error row is seen but *cleared* by the marker |
| turn 2, again | the model call that failed runs now — turn 1 is **not re-paid** | 11 `model_call`, … `final` |

## 5. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| Requeue = an appended `requeued` marker | Reset the run row and wipe/ignore the ledger | The ledger stays append-only (the whole project's rule), the history of the failure is kept, and replay keeps every paid model call. The marker is written FIRST, status flipped second: the other order lets a worker claim the run while replay still sees an uncleared verdict |
| Retries inside a step (Day 8) vs one re-ask as a real turn (Day 9) | — | A 503 is the same question again — noise, kept out of the transcript. A validation error is *new information the model needs* — so it must be a real conversation turn, and `repair_requested` makes it replayable |
| Rung 1 = extract the first balanced JSON object | Regex for code fences, `json5`, "fix trailing commas" libraries | One mechanism covers fences *and* prose (both are noise around an object); brace-counting that respects strings is 25 lines and has no dependency. Anything subtler is rung 2's job — the model fixes its own answer |
| One re-ask per run (`repair_attempts = 1`) | Keep re-asking until valid | The second identical failure says the model can't fix it; more re-asks burn budget for nothing. It's a setting, not a constant |
| The reaper runs in idle workers | A separate janitor process / cron | Zero new infrastructure; the UPDATE is indexed by `runs_claimable_idx` and usually matches nothing. The status flip is the atomic claim on the reap, so racing workers can't double-reap |
| `max_attempts = attempt_count + 5` on requeue | Reset `attempt_count` to 0 | `attempt_count` is the audit trail of what actually happened (Day 2's "claimed once" proof reads it); raising the ceiling preserves it |
| `repaired_by` lives in the `final` ledger row, stripped from the run's result | Tag the stored result | Consumers of the record (the API, `companies`) get a clean record; the evidence stays in the ledger, where evidence lives |

## 6. Crash walk

| Worker/operator dies… | What's in the DB | What happens next |
|---|---|---|
| after `repair_requested`, before the re-ask | the marker row | replay rebuilds the correction message, counts the used re-ask, and the next worker's first model call *is* the re-ask |
| after the re-ask's `model_call`, before judging | an unjudged `end_turn` reply, `repairs_used = 1` | replay hands it to the judge; a still-broken answer goes straight to `error` (no second re-ask) |
| requeue dies between marker and status flip | `requeued` row, run still `failed`/`dead` | harmless: requeue again; the flip is guarded by `status IN ('failed','dead')` |
| reaper dies between the flip and the marker step | run `dead`, no error row | `runs.error` carries the reason; `dlq list` falls back to it |
| two operators requeue at once | both race for the marker's index | one wins; the loser gets the fencing error and reports "another requeue was writing"; the flip is idempotent |
| worker killed mid-run after a requeue | the usual Day 6 story | the lease expires, the next claim replays — `requeued` is just another row |

## 7. The proofs

`python -m chaos.repair_dlq_test --n 25 --exhausted-n 5` → numbers in
`chaos/repair_dlq_results.json`, run on local Postgres 16 in a cloud container
(not Neon), 3 Docker-less workers, 166 s. All 20 unit tests pass too (92 total).

**Repair** (25 runs per breakage kind):

| Final answer | Outcome | How |
|---|---|---|
| clean | 25/25 succeeded | rung 0; **0 repairs on clean runs** (the ladder never fires for nothing) |
| fenced | 25/25 succeeded | all rung 1 (`extracted_json`) — **free, no extra model call** |
| prose | 25/25 succeeded | all rung 1, free |
| bad_year | 25/25 succeeded | all rung 2 (`model_reask`) — exactly 25 re-asks paid, one each |
| hopeless | 25/25 failed | exactly one re-ask each, then an honest `invalid_final_answer` — the ladder never loops |

**Repair success rate on repairable breakage: 75/75.** Before Day 9, a strict
parser failed all 100 broken runs.

**Outage → DLQ → requeue** (25 runs, model failing 90% per call): 24 failed
`model_unavailable` (1 got lucky). `requeue_run` requeued all 24; healthy
workers then finished **25/25**. Every requeued run **resumed from its ledger**
(the `requeued` marker sits at exactly the next index after the error row; 0
restarted), keeping the 8 model calls paid before the outage. 0 ledger problems.

**Kill-until-exhausted → reaper → requeue** (5 runs, lease 3 s): each run's
owner was `kill -9`'d on all 5 claims → the reaper marked all 5 `dead`
(`max_attempts_exhausted`), requeue raised the attempt ceiling, and **5/5 then
succeeded** with clean ledgers — some of those final ledgers carry 3 workers'
ids and both markers, and `check_run` accepts them.

## 8. Questions to answer yourself

1. Why is a 503 retried *inside* a step (invisible to the transcript) while a
   validation failure becomes a real ledger row and conversation turn?
2. Why must the `requeued` marker be written before the status flips to
   `pending`, not after?
3. The reaper only reaps runs whose lease has expired. Why is that condition
   there, when the attempts are already spent?
4. A requeued `budget_exceeded` run without `--budget` fails again instantly.
   Why doesn't requeue reset the spend?
5. Why does rung 1 count braces instead of using a regex for code fences?
6. The zombie questions, one more: a worker dies right after writing
   `repair_requested`. How many re-asks does the run get in total?
7. Why does `repaired_by` go in the ledger's `final` row but not into the
   stored company record?

<details>
<summary>Answers</summary>

1. A 503 carries no information — the same question again may just work, so
   failed attempts would only pollute a replayed conversation. A validation
   error is new content the model must read; hiding it would make replay
   rebuild a *different* conversation than the one the model actually saw.
2. A worker can claim the instant the run is `pending`. If it claims before the
   marker exists, replay sees the old `error` row, returns failed, and the
   worker marks the run failed again — the requeue would eat one attempt and do
   nothing. Marker-first means a crash in between leaves a harmless marker.
3. `attempt_count` is incremented at claim time, so the *final* attempt's owner
   is still working while the count reads "spent". Reaping on the count alone
   would dead-letter a run that is about to succeed. An expired lease proves
   the owner is gone.
4. Spend is summed from the ledger's `model_call` rows — the money was truly
   paid, and pretending otherwise would break the budget's honesty. Raising the
   budget is the operator's explicit decision (`--budget`).
5. The failure modes are "JSON wrapped in noise", and fences are just one kind
   of noise. One brace-counting pass handles fences, prose, and both at once;
   a fence regex handles only fences. Strings with `{` inside are why it can't
   be a naive count.
6. One. The marker is in the ledger, so replay counts it: `repairs_used`
   starts at 1 for the next worker, and the judge goes straight to `error` if
   the re-ask's answer is still broken. The ladder's budget survives crashes
   like everything else — because it's in the ledger.
7. The record is the product; how it was salvaged is operations. Mixing them
   would make every consumer of the record handle a field that isn't about the
   company. Replay strips it so a resumed run returns the same clean result a
   fresh run would.
</details>

## 9. Interview lines

- "A final answer that fails validation goes up a repair ladder: a free
  deterministic JSON extraction, then one re-ask where the model sees its own
  validation errors as a real, replayable conversation turn — transient noise
  stays out of the transcript, but new information goes in it."
- "The dead-letter queue is the runs table itself: exhausted runs are swept to
  status `dead` by idle workers, and a requeue is an append-only ledger marker
  that clears the verdict — so a requeued run resumes from its ledger and never
  re-pays the model calls made before the failure."
- "Day 8's `model_unavailable` failures become operational: list them grouped
  by reason, requeue them after the outage, and the ledgers prove the runs
  continued at the exact step where the outage cut them off."
