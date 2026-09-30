# Chaos harness — 200 runs, a worker killed in each

## 1. The picture

Days 6 and 7 were each proven with a handful of hand-aimed kills. That shows
the design *can* recover; it doesn't show it *always* does. The chaos harness
is the difference between "I tried it and it worked" and "I killed a worker at
a random moment in 200 runs and here are the numbers".

**Analogy:** a fire drill that isn't announced. You don't pick the moment when
everyone is ready; the alarm goes off at random, and you count how many people
got out and how long it took.

## 2. Runtime story: one iteration

| Step | What the harness does | What Relay does |
|---|---|---|
| 1 | `POST /v1/runs` with a unique domain (`chaos-17-a3f2.example`) | the API writes the run + step 0; a worker claims it |
| 2 | sleeps a random 0–10 s | the worker works through the run (fake model sleeps 1.5 s per call) |
| 3 | reads `runs.lease_owner`, `docker kill`s that container, notes the database time | the worker dies mid-step; nothing is cleaned up |
| 4 | waits for `succeeded` / `failed` | ~15 s later the lease expires; another worker claims (attempt 2), replays, finishes |
| 5 | reads the ledger, `tool_invocations`, `companies`; runs `check_run`; restarts the killed container | — |
| 6 | appends one line to `chaos/iterations.jsonl` | — |

If the run hadn't been claimed yet at the kill moment, nothing is killed and the
iteration is counted as "no kill (before claim)". A run already finished would
count as "no kill (after finish)"; none did.

## 3. The results (N = 200)

Setup: fake model and fake search/fetch tools, real Postgres (Neon) writes, 3
worker containers, `LEASE_SECONDS=15`, `FAKE_DELAY_SECONDS=1.5`, kill moment
uniform in 0–10 s after the POST. Total wall time 103 minutes.

| Metric | Result |
|---|---|
| Iterations | 200 |
| Killed mid-run | 196 (4 not killed: the run wasn't claimed yet) |
| Killed runs that still ended `succeeded` | **196 / 196 (100%)** |
| Runs with a repeated model call (`check_run`) | **0** |
| Domains with more than one company row | **0** |
| `save_company` effects committed more than once | **0** |
| Runs whose company row count isn't exactly 1 | 0 |
| Ledger rows written before the kill, replayed instead of redone | 1,212 |
| Model calls made before the kill, not paid again | 302 |
| Tool calls re-executed after a crash (killed between `tool_call` and `tool_result`) | 30 |
| …of which `save_company` re-issued and still executed once | 13 |
| Tokens a cold restart would have spent again | 492,938 of 1,440,982 (**34%** of the killed runs' tokens) |
| Time from kill to the new worker's first step | median **15.8 s**, p90 16.9 s, range 10.3–17.9 s |

Where the kills landed (last ledger row before the kill): 40 right after
`run_started`, 27 after a `model_call`, 30 after a `tool_call` (mid-tool), 99
after a `tool_result`. So every recovery path was hit many times, including the
write tool's receipt path.

**Reading the numbers honestly:**

- **Time to resume ≈ the lease.** It's 15 s because `LEASE_SECONDS=15` in the
  test. With the production default of 90 s, a crashed run would wait up to 90 s.
  Resume time is a setting, not a property of the code.
- **34% tokens saved** depends on where the kills landed: a kill just after
  step 0 saves nothing, a kill before the final answer saves almost everything.
  With a random kill point, about a third of a run's tokens would have been paid
  twice without replay.
- **It's the fake model.** This measures crash recovery (leases, replay,
  idempotency), not answer quality. Real model calls would be slower and pricier,
  which makes the replay savings worth more, not less.
- **Not covered:** the "final written, then killed before `mark_succeeded`"
  window (0 hits: it's one database round trip wide) and a stale worker waking up
  after a `docker pause` (tested by hand on Day 3, not in this harness).

## 4. Design decisions

| Choice | Alternative | Why |
|---|---|---|
| One run at a time | Many runs in parallel | One kill hits exactly one run, so each result is clean. Throughput isn't what's measured |
| Kill the container that owns the run (`lease_owner` → container id) | Kill a random worker | A random kill would often hit an idle worker and prove nothing |
| Kill moment from the database clock (`SELECT now()` after the kill) | The laptop clock | `steps.created_at` uses the database clock; the Docker VM clock drifted ~15 s earlier |
| Pre-kill rows = `created_at` ≤ kill time | Rows by the killed worker's id | The restarted container gets the same id, so ids can't tell the two apart |
| The killed container is restarted *after* the run finishes | Right after the kill | The run then resumes on a different worker; the restart doesn't race the takeover |
| One JSON line per iteration, appended immediately; the harness resumes from the line count | Write everything at the end | A stop (laptop sleep, closed window) loses at most one run |
| "No kill" iterations counted separately | Drop them | Hiding them would inflate the kill count |
| `FAKE_DELAY_SECONDS=1.5` | 0 | Without a delay a run is ~4 s and most kills land after it finished |

## 5. Crash walk: what if the harness itself dies?

| Harness dies… | State | Next start |
|---|---|---|
| after POST, before the kill | a run finishes normally, no line written | the iteration is redone with a new domain; the orphan run isn't counted |
| after the kill, before the line | the run resumes and finishes on its own; the killed worker stays down | `ensure_workers()` at start brings it back; the iteration is redone |
| after writing the line | nothing pending | continues at the next index |

## 6. The proof

- Dry run first: `python -m chaos.crash_test --n 10` → 9 killed, 9 succeeded, no
  duplicates (kept as `chaos/dryrun_results.json`, not counted in the 200).
- Full run: `python -m chaos.crash_test --n 200` in its own window, 103 minutes,
  first run `32fd9fa3`, last `52890749`. Raw lines: `chaos/iterations.jsonl`;
  summary: `chaos/results.json`.
- Re-create the summary from the raw lines: `python -m chaos.crash_test --summary`.

## 7. Questions for Sakshi Roy

1. Why is the median time to resume almost exactly the lease length?
2. Why does the harness kill the *owner* of the run instead of a random worker?
3. Why are rows split into "before/after the kill" by timestamp and not by worker id?
4. 30 tool calls were re-executed. Why isn't that a failure?
5. Why does "34% of tokens saved" depend on where the kill lands?
6. What does this harness *not* prove?

<details>
<summary>Answers</summary>

1. After the kill, nobody renews the lease. The run only becomes claimable when
   `lease_expires_at` passes, up to 15 s after the last heartbeat; then a polling
   worker (every 0.5 s) claims it. With a 90 s lease it would be ~90 s.
2. Killing an idle worker changes nothing about the run. Only killing the owner
   tests recovery.
3. The killed container is restarted with the same container id, so its worker
   id (`<id>:1`) is the same string. The database clock at the kill moment is
   the only clean boundary.
4. They were read-only tools (safe to repeat) or `save_company`, which went
   through its receipt: in all 13 cases it was re-issued and still executed
   once. The metric that must be 0, "effects committed more than once", is 0.
5. The saving is the work done before the kill. A kill at step 0 saves nothing,
   a kill before the final answer saves almost the whole run. Random kill
   points average out to about a third.
6. Answer quality (it's the fake model), a real provider's latency and errors,
   the one-round-trip "final written, then killed" window, stale workers waking
   up (the `docker pause` case), and many runs at once.

</details>

## 8. Interview lines

- "I wrote a chaos harness that starts a run, kills the worker that owns it at a
  random moment, and checks the ledger. Over 200 runs, 196 were killed
  mid-run and all 196 finished, with zero repeated model calls and zero duplicate
  company rows."
- "Recovery time is the lease: 15 seconds in the test. It's a setting, and I'd
  tune it against how long a real model call takes."
- "Replay avoided re-paying about a third of the tokens in the killed runs; with a
  real model that's real money, and the number depends on where crashes land."
