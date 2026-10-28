# The client Idempotency-Key — the first idempotency layer

## 1. The picture

Day 7 made sure a *worker* never commits a write twice. But one hole was still
open, before any worker is involved: the *client*. A laptop on flaky Wi-Fi sends
`POST /v1/runs`, the run is created, and the reply is lost on the way back. The
client has no idea whether it worked, so it does the only sane thing: it sends
the request again. Without protection that's a second run, a second bill, and
a second company row update.

**The courier analogy.** When you book a courier and the page freezes, you press
"book" again. A good courier service gave your booking a reference number before
you pressed anything; the second press finds that reference and shows you the
booking you already have. Nobody comes to your door twice.

The client sends `Idempotency-Key: <any unique string>` (a UUID is typical).
The first request creates the run and stores the key next to it, **in the same
commit**. Any repeat with the same key and the same body gets the same run back,
marked `Idempotent-Replayed: true`. The same key with a different body is a
client bug and gets `422`.

With this, all three layers from the README's mechanism table exist:

| Layer | Protects against | Since |
|---|---|---|
| client `Idempotency-Key` → `idempotency_keys` | a client retrying `POST /v1/runs` | now |
| per-tool-call key → `tool_invocations` | a worker re-running `save_company` after a crash | Day 7 |
| `UNIQUE (tenant_id, domain)` on `companies` | everything above failing | Day 7 |

## 2. Each file's job

| File | One line |
|---|---|
| migration 007 | `idempotency_keys (tenant_id, key) PRIMARY KEY → run_id`, plus a SHA-256 of the request body |
| `relay/api/repo.py` | `create_run(..., idempotency_key)`: under the existing per-tenant advisory lock, look the key up first: same body → return that run (replayed); other body → `IdempotencyMismatch`; unknown → create run + step 0 + key row in one transaction |
| `relay/api/routes/runs.py` | reads the header, validates it (1–255 printable ASCII → else `400`), maps mismatch → `422`, sets `Idempotent-Replayed: true` on a replay |
| `relay/api/static/index.html` | every Start click sends a fresh `crypto.randomUUID()` as the key |
| `tests/test_idempotency_key.py` | 7 tests (below) |

## 3. Runtime story: a retry after a lost reply

| Moment | What happens | Database |
|---|---|---|
| 1 | client POSTs `{"domain":"idem-live.example"}` with key `K` | advisory lock (tenant) → key unknown → cap check → run + step 0 + key row, **one commit** |
| 2 | the 202 is lost on the network | — |
| 3 | client retries: same key `K`, same body | lock → key found, body hash matches → **return the stored run id**; nothing inserted |
| 4 | client gets `202`, the same `run_id`, `Idempotent-Replayed: true` | still exactly 1 run |

Verified live on uvicorn: run `4f186a0d`, first call and retry returned the same
id, the retry carried the replay header, the same key with another domain got
`422`, and `SELECT count(*)` found 1 run.

## 4. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| Key row in the same transaction as the run | Store the key after the run commits | A crash between the two would leave a run with no key, and the retry would create a second one. One commit means a key without its run (or the reverse) can't exist, which is the Day 1 "enqueue and create in one transaction" rule again |
| Reuse the Day 10 per-tenant advisory lock | Rely on the primary key and catch the unique violation | The lock is already held for the cap check, so two racing retries are serialised for free: the second one waits a few ms and finds the first one's committed key. The primary key still backs it up |
| Look up the key **before** the concurrency cap | Cap first | A replay creates nothing, so refusing it with 429 would make a client at its cap think its run was never made |
| Body hash, mismatch → 422 | Ignore the body; or overwrite the run | Same key with a different body means the client reused a key by mistake. Returning the old run silently would hide that bug; 422 matches the IETF Idempotency-Key draft |
| Keys per tenant (`PRIMARY KEY (tenant_id, key)`) | Global keys | Two tenants can't collide, and a key can't be used to probe whether another tenant's request exists |
| Keys live as long as their run (`ON DELETE CASCADE`), no 24 h expiry | Expire like Stripe | Simplest honest version; nothing deletes runs today. An expiry is one `WHERE created_at > now() - interval` away if ever needed |
| Replay returns the run's **current** status | Byte-for-byte the original response | The useful answer to "did my run get made?" is the run as it is now; the id is what matters |

## 5. Crash walk

| The API dies… | State | The client's retry |
|---|---|---|
| before the commit | nothing written (the transaction rolled back, lock released) | creates the run, once |
| after the commit, before the reply | run + step 0 + key row | replayed: same run |
| two retries race | the advisory lock orders them | first creates, second replays |

## 6. The proofs

`tests/test_idempotency_key.py`, 7 tests (116 total):
- same key + same body → same run id, replay header on the second, 1 row
- same key + different body → 422, nothing created
- no key → two runs (old behaviour unchanged)
- the same key string in two tenants → two different runs
- a tenant at its cap: new work → 429, but a replay → 202 with the original run
- malformed keys (256 characters, a tab, non-ASCII) → 400
- **8 parallel retries with one key → eight 202s, one run id, 1 row**

## 7. Questions for Sakshi Roy

1. Why must the key row commit in the same transaction as the run?
2. A tenant is at its concurrency cap and retries a POST that already
   succeeded. Why does it get 202 and not 429?
3. Why does the same key with a different body get 422 instead of the
   original run?
4. Day 7 already has idempotency keys. Why isn't that enough here?
5. Two retries arrive at exactly the same moment. Walk through why only one
   run is created.

<details>
<summary>Answers</summary>

1. If the run committed first and the API died before the key was written,
   the retry would find no key and create a second run: the exact bug the
   header exists to prevent.
2. A replay creates nothing, so the cap has nothing to protect. Refusing it
   would tell the client its run doesn't exist when it does, and the client
   would keep retrying.
3. It means the client reused a key for a different request, which is a bug.
   Quietly returning the old run would hide it; 422 makes it visible.
4. Day 7's keys protect *inside* a run (a worker re-running `save_company`).
   This protects the step before: a client retrying the request that
   *creates* the run. Different hole, same idea.
5. Both take the tenant's advisory lock; one waits. The first finds no key,
   creates run + key and commits, releasing the lock. The second now finds the
   committed key with a matching body hash and returns that run.
</details>

## 8. Interview lines

- "Idempotency has three layers in Relay: a client Idempotency-Key on run
  creation, a per-tool-call key in front of every write, and a unique constraint
  on the output table, so a duplicate needs all three to fail at once."
- "The client key commits in the same transaction as the run it created, so a
  crash can never leave one without the other; eight parallel retries with one
  key produce exactly one run."
- "A replay is checked before the concurrency cap, because refusing a request
  that creates nothing would tell the client its run doesn't exist."
