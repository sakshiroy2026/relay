# Day 11 — Server-sent events: the ledger pushed, not polled

## 1. The picture

The demo page asked "anything new?" once a second — two HTTP requests per
second per viewer, and every step shows up half a second late on average.
Day 11 turns that around: the moment a ledger row commits, Postgres rings a
bell, the API pushes the new state down an open connection, and the page
just listens.

**The analogy:** instead of the family calling the ward desk every minute for
news, the desk keeps one phone line open and speaks the moment the chart
changes. The chart (the ledger) is still the only source of truth — the phone
call just says "look again", and what's read out is always read *from the
chart*.

Measured end to end: **median 3.7 ms** from a step's insert commit to the
parsed event in the client, p90 6.7 ms, across 65 steps of 5 live worker runs.
Polling at 1 s averaged ~500 ms.

## 2. How the bell works

| Piece | What it does |
|---|---|
| migration 006 | `AFTER INSERT ON steps → pg_notify('relay_steps', run_id)` — a trigger, so the harnesses' direct inserts ring it too and `append_step` stays untouched. `pg_notify` fires **on commit**: a rolled-back insert rings nothing, so an event can never describe data that doesn't exist |
| `relay/api/events.py` | `GET /v1/runs/{id}/events`: a generator holding one dedicated LISTEN connection. On each bell for its run (or a 2 s tick for quiet changes like a lease expiry), it re-reads the run + steps through the same tenant-scoped repo as the polling endpoints and emits `event: update` only when a fingerprint (steps count, status, owner, spend) changed. Terminal status → `event: done`, stream closes |
| the page | `fetch` streaming with a hand-rolled SSE parser — **not `EventSource`**, which cannot send the `Authorization` header. Any stream problem (old browser, proxy cut, 404) falls back to the Day 7 polling loop; the heading shows ", pushed" or ", polling" |

Same auth and rate limit as every route (one stream = one request); another
tenant's run gets `event: gone`, matching the 404 story.

## 3. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| Push "look again", send state re-read from the database | Put the new step's data in the NOTIFY payload | The payload would be a second copy of the truth (size-limited, unordered under concurrency). Re-reading on each bell means an event is always consistent with the ledger — replay's lesson applied to the UI |
| A trigger rings the bell | `pg_notify` called in `append_step` | Every writer rings it — workers, harnesses, the requeue CLI — and the proven ledger code is not touched |
| One channel, payload = run id | A channel per run | LISTEN/UNLISTEN churn per stream for no gain; a wake for someone else's run costs one fingerprint comparison |
| fetch-streaming on the page | `EventSource` | EventSource cannot set `Authorization`; the common workaround (key in the query string) puts the key in access logs. The parser is 20 lines |
| Keep polling as the fallback | SSE only | Old browsers, aggressive proxies, and any server hiccup degrade to the already-proven path instead of a blank page |
| A dedicated connection per stream | A pooled connection | LISTEN state belongs to a connection; parking one in the pool would poison it. At demo scale (a handful of viewers) this is fine; at real scale one shared listener task fans out to in-process queues — interface unchanged |
| The LISTEN connection goes to Neon's *direct* host (`listen_conninfo` strips `-pooler`) | Use `DATABASE_URL` as is | Neon's pooled host is PgBouncer in transaction mode: each statement may land on a different server connection, so the LISTEN is forgotten and no bell ever arrives. Found when the branch was first run on Neon (the stream still worked, but only at the 2 s tick) |

## 4. Crash walk

| Dies… | What happens |
|---|---|
| the viewer closes the tab | the generator is closed, its LISTEN connection dropped — nothing server-side remembers the viewer |
| the API restarts mid-stream | the fetch errors → the page falls back to polling, which works immediately; refreshing re-opens the stream |
| the worker dies mid-run | nothing rings for ~lease seconds; the 2 s tick still re-reads, so the lease-expiry takeover appears as an `update` (new owner, new attempt) and the banner fires exactly like under polling |
| a step insert rolls back (fencing) | `pg_notify` never fires — it is transactional — so no ghost event |

## 5. The proofs

- Browser end-to-end (Playwright, real uvicorn + worker): the page streamed a
  full 14-step run with `", pushed"` showing, the final record and company
  panels filled, **zero console errors**.
- Latency: 5 worker runs, 65 worker-written steps — **median 3.7 ms, p90
  6.7 ms, max 9.9 ms** from insert commit to parsed client event (same-host
  clocks; local Postgres in a cloud container, not Neon).
- 5 new tests (109 total): a step inserted mid-stream arrives without a new
  request; a terminal run yields `update` then `done` and the generator ends
  on its own; another tenant's run yields `gone`; the route 401s without a
  key; the trigger's notification carries the run id.
- On Neon (Windows host, 28 Oct): the trigger test failed at first — no
  notification through the pooler — and passes since `listen_conninfo`.
  Full suite on Neon: 117 passed.

## 6. Questions for Sakshi Roy

1. Why does the event carry state *re-read from the database* instead of the
   new step that rang the bell?
2. Why is it important that `pg_notify` fires on commit, not on execute?
3. Why fetch-streaming instead of the purpose-built `EventSource`?
4. The worker dies and another takes over. No insert happens for 15 seconds.
   How does the page still show the takeover?
5. Why one NOTIFY channel for all runs instead of one per run?
6. What would have to change for 10,000 concurrent viewers?

<details>
<summary>Answers</summary>

1. The payload would be a second, size-limited copy of the truth that can
   arrive out of order under concurrency. "Bell + re-read" can never disagree
   with the ledger — the same reason replay reads the ledger instead of
   trusting memory.
2. Fencing: a zombie's insert that loses to the UNIQUE constraint rolls back.
   If NOTIFY fired on execute, viewers would see a ghost step that was never
   committed.
3. `EventSource` cannot send the `Authorization` header, and putting the key
   in the URL writes it into every access log. fetch gives the same stream
   with real headers for ~20 lines of parsing.
4. The stream also ticks every 2 s and re-reads; the takeover changes
   `lease_owner`/`attempt_count`, the fingerprint changes, an `update` goes
   out. The bell is the fast path, not the only path.
5. Channels are connection-level state; per-run channels mean LISTEN/UNLISTEN
   churn and no real saving — a stray wake costs one string compare and, at
   worst, one cheap re-read.
6. One LISTEN connection and one snapshot query *per viewer* stop scaling.
   One shared listener task per API process would fan bells out to in-process
   subscriber queues, and hot runs' snapshots would be built once per bell,
   not once per viewer. The endpoint's contract wouldn't change.
</details>

## 7. Interview lines

- "The live page is LISTEN/NOTIFY end to end: a trigger rings on every ledger
  commit, the stream re-reads through the same tenant-scoped repository as
  the REST endpoints, and the event can never disagree with the ledger —
  median push latency 3.7 ms against ~500 ms for the old 1-second polling."
- "NOTIFY fires on commit, which matters here: a fenced zombie's rolled-back
  insert rings no bell, so viewers can't see a step that never happened."
- "I used fetch-streaming instead of EventSource for one concrete reason:
  EventSource can't send an Authorization header, and the usual workaround
  leaks the API key into access logs. Polling stays as the automatic
  fallback, so the degraded mode is the previously proven mode."
