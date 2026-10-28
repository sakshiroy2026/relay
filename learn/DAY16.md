# Day 16 — Idempotency, fencing and throughput, measured

## 1. The picture

Relay's safety rests on three small mechanisms that were each proven once, by
hand: the receipt book for write tools (Day 7), the fencing constraint that
stops a stale worker (Day 3), and the `SKIP LOCKED` claim query (Day 2). One
proof shows a mechanism *can* work. Day 16 leans on each one much harder than
real traffic would, many times, and counts what happens.

**The hospital analogy, one more time.** Day 3's test was one nurse dozing off
mid-shift and waking to find that someone else had taken over her patient.
Day 16 does it 100 times, at random moments: she might wake while the new
nurse is still reading the chart, or long after the patient has gone home. The
chart (the ledger) must still read as one coherent story every time, and we
note which rule caught her: the handover check at the start of her round (the
heartbeat), the page she tried to write already being filled in (the UNIQUE
constraint), or the discharge desk refusing her signature (the guarded finish).

No mechanism changed today. This is measurement: three harnesses, new tests,
and a stricter ledger checker.

## 2. The three tests, one row each

| Test | What it hammers | Pressure applied | Must be 0 |
|---|---|---|---|
| A2 idempotency (`chaos/idempotency_test.py`) | `save_company`'s receipt (Day 7) | 4 processes calling with the same key at the same instant, then `kill -9` while the write is open | keys executed twice; company rows ≠ 1; a company row without its receipt (or a receipt without its row) |
| A3 fencing (`chaos/fencing_test.py`) | lease + `UNIQUE(run_id, step_index)` + guarded finish (Day 3) | `SIGSTOP` on the owner at a random moment, takeover, `SIGCONT` a random 0–2 s later | a stale worker finishing a run; a ledger `check_run` rejects; a run not `succeeded` |
| A4 throughput (`chaos/throughput_test.py`) | the claim query (Day 2) | 1 → 16 workers draining a batch; the claim query timed with 100,000 finished runs in the table | a run claimed twice; a ledger problem |

`SIGSTOP` is what `docker pause` does to a container's processes: the process
is frozen and doesn't know it. `SIGCONT` is `docker unpause`.

## 3. Each file's job

| File | One line |
|---|---|
| `chaos/harness.py` (new) | Shared plumbing: create runs without the API, spawn workers with log files, wait for terminal runs, read the DB clock |
| `chaos/idempotency_test.py` (new) | A2: racers and kills on `save_company`, through the real `dispatch_tool` path |
| `chaos/fencing_test.py` (new) | A3: freeze the owner, let another worker take over, wake the frozen one, classify what stopped it |
| `chaos/throughput_test.py` (new) | A4: runs/s across worker counts, plus the claim query's cost with a big backlog |
| `scripts/check_run.py` | Same two checks as before, plus ledger *shape* checks (below); the check is now a pure function, `find_problems` |
| `tests/test_check_run.py`, `test_fencing.py`, `test_idempotency_race.py`, `conftest.py` (new) | 15 tests; the database ones use parked runs (`status = 'dead'`) so they never touch a real queue |

**The stricter checker.** Before, `check_run` checked "no gaps, no repeated
model call". A stale worker could in principle break the ledger in other ways,
so it now also checks: step 0 is `run_started`; every `tool_call` /
`tool_result` answers the latest `model_call`; no tool call gets two results;
nothing comes after `final` / `error` / `budget_exceeded`. All old ledgers
pass it.

## 4. Runtime story: a zombie that wins the race (a real iteration)

The surprising case from the A3 runs (dry run, run `66e4ae4b`). Lease 3 s,
fake model 0.3 s per call. Three workers: `945` (the zombie), `944`, `943`.

| Moment | What happens | Ledger |
|---|---|---|
| 04.6 | `945` claims (attempt 1), writes turn 0 | 1 `model_call`, 2–3 search |
| ~04.8 | harness freezes `945` **inside its second model call** | — |
| ~07.8 | lease expired; `944` claims (attempt 2), replays 0–3, starts the model call for step 4 | — |
| ~08.1 | harness wakes `945`. Its model call finishes; it writes step 4 **before `944` does** | 4 `model_call`, 5–8 both fetches, all by `945` |
| 08.1 | `944`'s model call returns; its write of step 4 hits the constraint → `lease_lost`, it stops | (rejected) |
| 08.1 | `945` reaches the top of turn 2: heartbeat → the lease is `944`'s → `lease_lost`, it stops | — |
| ~11.1 | `944`'s lease expires unrenewed; `943` claims (attempt 3), replays 0–8, finishes | 9–13, `final` |

Nobody wrote anything wrong. Every row is a valid next step, every model reply
appears once, and there is one company row. But notice:

- The **stale** worker's writes were accepted (they landed first) and the
  **new** owner was the one fenced. Fencing doesn't promise "the newest worker
  wins". It promises **one writer per step index**, which keeps the ledger a
  single history whoever writes it.
- Two model calls were paid for and thrown away: `944`'s step-4 call, and
  nothing more from `945` (its call was the one kept).
- The run lost one extra lease (3 s): `944` stopped but still held the lease
  until it expired.

## 5. The proofs

All three were run on **local Postgres 16 in a cloud container (not Neon), 4
CPUs, Docker-less workers as plain processes**. That note is stored inside each
results file. Numbers on Neon would differ mainly by network latency.

### A2 — idempotency (`chaos/idempotency_results.json`, 82 s)

| Phase | Pressure | Result |
|---|---|---|
| `race` | 300 keys × 4 processes, same instant | 1,200 calls → **every key executed exactly once**; 324 writes rolled back by the receipt check, 576 calls answered straight from the receipt |
| `race_widened` | same, every write holds its transaction open 20 ms | overlap forced: 1,153 of 1,200 calls started the write, **853 rolled back, 300 committed** — exactly one per key |
| `kill` | 200 keys, writer `kill -9`'d at a random moment mid-write | 144 killed mid-write (receipt `in_flight`, write rolled back), 34 after commit, 22 before the receipt. **Write and receipt never disagreed.** Re-issuing all 200 keys: 166 executed (the unfinished ones), 34 answered from the receipt → **200/200 executed exactly once, one company row each** |

Every phase: keys executed more than once = **0**, domains with ≠ 1 company
row = **0**. All four racers always got the same answer.

### A3 — fencing (`chaos/fencing_results.json`, 100 iterations, 11 min)

Conditions: 3 workers, `LEASE_SECONDS=3`, fake model 0.3 s/call (a run ≈ 1.3 s),
`SIGSTOP` at a random moment inside the run, `SIGCONT` 0–2 s after takeover.

| Metric | Result |
|---|---|
| Owner frozen mid-run | 97 (1 run finished first; 2 held a lock, below) |
| Frozen-and-woken runs that ended `succeeded` | **97 / 97** |
| Runs finished or failed by a stale zombie | **0** |
| How the zombie was stopped | 81 × `UNIQUE(run_id, step_index)`, 14 × heartbeat, 2 × guarded finish |
| What the zombie lost to the constraint | 79 `model_call`, 2 `tool_call` |
| Zombie writes accepted because they landed *first* (§4) | 92 rows across 16 runs — in each, the **new owner** was the one fenced; ledgers all valid |
| Model calls paid for, then thrown away by fencing | 95 across 97 runs (the known cost: at most ~1 per zombie) |
| Ledger problems (`check_run`, incl. the new shape checks) | **0** |
| Keys executed twice / domains with ≠ 1 company row | **0 / 0** |
| Takeover time | median 3.02 s, p90 3.23 s — the lease, as designed |

The zombie mostly loses a `model_call`: with a 0.3 s model, a random freeze
almost always lands inside a model call, and the heartbeat before it has
already passed. The 14 heartbeat stops are freezes that lasted into the *next*
turn's start.

**The 2 "no takeover" iterations** (`02314445`, `cca360ff`): the freeze landed
inside one of the worker's short transactions, so the frozen connection kept a
lock on the `runs` row. `FOR UPDATE SKIP LOCKED` does what it says — it skipped
the locked row — so nobody could claim the run until `SIGCONT`. The woken
worker then finished its own run normally (`attempt_count = 1`, `succeeded`).
Not a safety hole (nothing was ever duplicated or lost), but a liveness note:
in production, `idle_in_transaction_session_timeout` / TCP keepalives bound how
long a frozen client can hold a lock.

### A4 — throughput (`chaos/throughput_results.json`)

**The claim query with a 100,000-run backlog** (EXPLAIN ANALYZE, 25 repeats,
rolled back afterwards):

| Situation | Median execution |
|---|---|
| ~1,600 rows in `runs`, partial index | 0.052 ms |
| +100,000 finished runs, partial index | **0.111 ms** |
| +100,000 finished runs, index dropped | **8.4 ms** (p90 13.7) — a full scan |

The Day 1 decision measured: finished runs are invisible to the partial index
(`WHERE status IN ('pending','running')`), so the queue stays O(claimable),
not O(history). Without it, every claim pays for the whole table — ~76× here,
and growing with every finished run.

**The sweep** (batch enqueued in one commit; a run = 4 model calls + 4 tool
calls; every point also re-checks claim exclusivity and every ledger):

| Fake model latency | Workers | Runs/s | Speedup ×1-worker | Efficiency |
|---|---|---|---|---|
| 0 s (Relay's own overhead) | 1 | 37.0 | 1.0 | — |
| | 2 | 73.5 | 2.0× | 99% |
| | 4 | 120.7 | 3.3× | 82% |
| | 8 | 178.6 | 4.8× | 60% |
| | 16 | 152.1 | 4.1× | 26% |
| 0.2 s (workers mostly waiting) | 1 | 1.18 | 1.0 | — |
| | 2 | 2.34 | 2.0× | 99% |
| | 4 | 4.70 | 4.0× | 100% |
| | 8 | 9.29 | 7.9× | 98% |
| | 16 | 18.43 | **15.6×** | **98%** |

Across all 2,430 runs in the sweep: runs claimed more than once = **0**,
ledger problems = **0**, every run `succeeded`.

Reading it honestly: with a 0 s model, workers are pure CPU + database round
trips, so the 4-CPU box tops out near 8 workers and 16 workers just fight for
CPU (p50 run duration doubles). With a 0.2 s model — still far faster than a
real one — workers spend most of their time waiting, and 16 workers on 4 CPUs
scale at 98% efficiency. The claim path is nowhere near being the bottleneck;
with a real model (seconds per call), it would matter even less.

## 6. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| Freeze with `SIGSTOP`, not kill | `kill -9` (already covered by the 200-run chaos harness) | A dead worker can't misbehave; only a *frozen* one wakes up believing it still owns the run. That's the case fencing exists for. |
| Parse the zombie's log line to see what stopped it | Add instrumentation to the worker | Day 3's code (claim, fencing, guarded finish) stays untouched. The kind of write it lost is recovered by replaying the ledger up to that index (`next_kind`), because rows never change. |
| A2 widens the write window (`pg_sleep` inside the transaction) | Only race the natural path | The natural path rarely overlaps for long. Widening *guarantees* every racer gets past the receipt check, which forces the "loser rolls back" branch. Both are measured. |
| Claim cost measured inside a transaction that is rolled back | Leave 100k rows behind | No clean-up needed, safe on a shared database, and the dropped index comes back on its own. |
| Database tests use parked (`dead`) runs and never call the claim query | Test `claim_run` in pytest | The claim query takes *any* pending run, so on a shared database a test could steal a real one. Claim exclusivity is checked in A4 instead, where the harness owns every run. |

## 7. Crash walk (the boundaries these tests aimed at)

| A worker freezes or dies… | What's in the DB | What happens next |
|---|---|---|
| after its heartbeat, before the model replies | nothing new | the new owner works; the zombie wakes, gets its reply (paid), and loses the write to the constraint, unless it gets there first (§4) |
| holding an uncommitted step insert | a row lock on that step index | the new owner's insert of that index **waits** for the zombie; on wake the zombie commits and the new owner is fenced (in production, `idle_in_transaction_session_timeout` would bound the wait) |
| between "write committed" and `tool_result` (A2 kill) | receipt `ok` + company row | re-issue returns the stored receipt and doesn't write |
| mid-write, transaction open (A2 kill) | receipt `in_flight`, **no** company row (rolled back) | re-issue writes once |
| right before `mark_succeeded` | `final` in the ledger | new owner replays, sees `final`, finishes it; the zombie's finish is refused by `lease_owner = me` |

## 8. Questions for Sakshi Roy

1. In §4 the zombie's writes were accepted and the new owner was fenced. Why is
   that still correct?
2. Why can't the heartbeat alone stop a frozen worker from paying for one more
   model call?
3. A2's racers all got the same answer, but the JSON strings differed. Why?
4. Why does dropping `runs_claimable_idx` make the claim query ~200× slower
   with 100k finished runs, while 0 vs 100k finished runs barely matters with it?
5. Why do the database tests never call `claim_run`?
6. In A2's kill phase, why must every `in_flight` receipt have *no* company row?
7. With a 0 s fake model, adding workers past the CPU count stops helping; with
   a 0.2 s model it keeps helping. Why?

<details>
<summary>Answers</summary>

1. Rows 4–8 were valid next steps from the ledger as it stood, and the
   constraint guaranteed nobody else could write those indexes. The ledger is
   one history no matter who wrote each row. The new owner learned it was
   fenced on its first write and stopped before doing anything.
2. The heartbeat runs at the *top* of a turn. A worker frozen just after a
   successful heartbeat has already decided to call the model; when it wakes it
   finishes the call (the provider bills it) and only then tries to write.
   Fencing protects the ledger, not the bill: at most one call per zombie.
3. `jsonb` stores object keys in its own order. The winner returns the dict it
   just built; everyone else gets the stored receipt back from `jsonb`. Same
   keys and values, different order.
4. Without the partial index the query must scan every run to find the few that
   are claimable. The partial index only contains `pending` / `running` rows,
   so finished runs are invisible to it, however many pile up.
5. The claim query takes the oldest claimable run, whoever it belongs to. On a
   shared database (Neon, with Docker workers up) a test would steal a real run.
6. The write and "receipt ok" commit in one transaction. A process killed with
   the transaction open commits neither; Postgres rolls back everything when
   the socket dies. A company row with an `in_flight` receipt would mean they
   committed separately.
7. With a 0 s model each worker is busy all the time with Python and database
   round trips, so 4 CPUs are the ceiling. With a 0.2 s model each worker
   spends most of its time waiting, so more workers fill the waiting time. That
   is the realistic case: a real model call takes seconds.
</details>

## 9. Interview lines

- "I didn't just build idempotency — I raced it: 4 processes per key with the
  write window held open so every racer got past the receipt check, plus 200
  `kill -9`s mid-transaction. 500 keys, every one executed exactly once, and
  the receipt and the row never disagreed, because they commit together."
- "The fencing test freezes the run's owner with SIGSTOP and wakes it after a
  takeover, 100 times. The zombie never finished a stale run; it was stopped by
  the unique constraint 81 times, the heartbeat 14, the guarded finish twice —
  and in 16 runs its write landed *first* and the new owner was the one fenced,
  which is still correct: fencing guarantees one history, not who writes it."
- "A partial index on claimable statuses keeps the claim at 0.1 ms with a
  100,000-run backlog — dropping it makes the same query 76× slower. And with
  0.2 s model latency, 16 workers on 4 CPUs scale at 98% efficiency, because
  the workers are waiting on the model, not on the queue."
