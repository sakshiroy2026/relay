# Day 10 — Full API keys, tenant scoping, rate limits

## 1. The picture

The safety slice (learn/SAFETY.md) had one key in `.env`: a front-door lock
with a single master key. Fine before a public URL; useless the moment there
are two customers, because the key only says *allowed*, never *who*.

**The hotel key card.** A card has a printed number and a chip. The desk looks
the room up by the printed number (fast), then the chip must match (slow,
secure) — and the card opens *your* room, not "a room". Relay's key is
`rly_<prefix>.<secret>`: the prefix finds the row in `api_keys` (indexed
lookup; the secret never touches a query), bcrypt verifies the secret against
the stored hash, and the caller becomes a **Principal** — a tenant with its own
limits. Every piece of SQL the API runs now takes that tenant id as part of the
WHERE clause. And because bcrypt is deliberately slow (~0.3 s — that's the
security), a verified key is cached in-process for 60 s as a SHA-256 digest.

Two ceilings come with the tenant: **requests per minute** (don't hammer the
API) and **max concurrent runs** (don't drain the model budget) — the second
one lives in Postgres, because that's the one that guards money.

## 2. Each file's job

| File | One line |
|---|---|
| `relay/api/principal.py` (new) | Bearer key → `Principal(tenant_id, key_name, limits)`: env-key fallback (dev tenant), prefix lookup + bcrypt, 60 s cache of SHA-256(key); 503 when no keys exist at all |
| `relay/api/repo.py` (new) | The repository layer: every API read/write, tenant_id as a *required argument* — a route cannot forget scoping, the signature demands it |
| `relay/api/ratelimit.py` (new) | Per-tenant fixed window (one minute, in-process) → 429 + Retry-After |
| `relay/api/routes/runs.py`, `companies.py` | Routes now do HTTP only: one shared `guarded` dependency (auth → rate limit), repo calls, status codes |
| migration 005 | `tenants.max_concurrent_runs` (default 25), `tenants.requests_per_minute` (default 120) |
| `relay/api/auth.py` | **Deleted** — the safety slice's single-key check lives on as the env-key fallback inside principal.py |
| `scripts/seed.py` | `--tenant NAME`: create more tenants with their own keys (the bcrypt + prefix design it used all along is now actually read) |

## 3. Runtime story: one POST /v1/runs

| Step | What happens | Cost |
|---|---|---|
| 1 | `Bearer rly_ab12cd34.xK…` arrives | — |
| 2 | not the env key → SHA-256 of the whole key → cache miss | — |
| 3 | `SELECT … FROM api_keys JOIN tenants WHERE key_prefix = 'rly_ab12cd34' AND revoked_at IS NULL` | one indexed read |
| 4 | `bcrypt.checkpw(secret, stored_hash)` | ~0.3 s, **first request only** |
| 5 | Principal cached 60 s; rate-limit window counter +1 | — |
| 6 | `repo.create_run`: advisory lock on the tenant → count open runs → under the cap → run + step 0 in one commit | one transaction |

Measured live: the first request with a fresh key took **294 ms** (bcrypt), the
second **7 ms** (cache). Revoking a key takes effect within 60 s per API
process — that is the cache's documented trade-off.

**The cap under racing requests:** 8 parallel creates against a tenant with
cap 3 → exactly `202, 202, 202` and five `429`s, and the database holds exactly
3 runs. The count and the insert happen under `pg_advisory_xact_lock(tenant)`,
so two requests cannot both read "2 open" and both insert. The lock is
per-tenant: other tenants don't queue behind it.

## 4. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| Prefix + bcrypt hash (the blueprint's design; the schema existed since Day 1) | Store the key, or SHA-256 it | A database leak yields no usable keys: bcrypt is salted and slow by design. The prefix exists so the lookup is indexed without putting the secret in a query |
| 60 s in-process cache of SHA-256(full key) | bcrypt on every request / no cache | 0.3 s per request is a self-inflicted outage. SHA-256 means a memory dump of the cache contains no usable key. The cost: revocation lags ≤ 60 s |
| Env key kept as a fallback → dev tenant | Break it and reseed everything | The demo page, the chaos harness and every script keep working unchanged; the fallback is one constant-time compare in front of the real path |
| A repository layer, tenant_id as a required argument | Scope in each route (status quo) | "Forgot the WHERE tenant_id" stops being a possible bug: the only functions that touch the database won't compile a call without a tenant. Routes shrink to HTTP concerns |
| Concurrency cap in Postgres (advisory xact lock + count) | In-process counter | Money is guarded by this cap, so it must hold across every API process and survive restarts — same reasoning as the Day 8 breaker living in Postgres |
| Request rate limit in-process | Redis (the blueprint), or Postgres per request | One API process is today's deployment; a shared counter would be Redis for nobody. With N processes the effective limit is ≤ N× — written down, and the money-cap stays exact regardless |
| 404 (not 403) for another tenant's run | 403 Forbidden | 403 confirms the run id exists. Another tenant's rows must be indistinguishable from missing ones |

## 5. Crash walk

| Dies… | State | Next |
|---|---|---|
| API dies between count and insert | the advisory lock was transaction-scoped: rolled back with everything else | nothing leaked; the client retries |
| API restarts | key cache and rate windows are empty | first requests re-pay bcrypt once per key; limits refill — both harmless |
| two creates race at the cap | one holds the tenant's lock, the other waits ms | the second sees the first's committed run in its count: 429 |
| a key is revoked mid-cache | cached Principal keeps working ≤ 60 s in that process | documented; `_cache.clear()` on demand if it ever matters operationally |

## 6. The proofs

- `tests/test_api_auth_tenancy.py` (7 tests, 99 total): no/wrong/short key →
  401 (`/healthz` stays open); wrong secret under a real prefix → 401; revoked
  key → 401 after the cache clears; tenant A's run → 404 and empty steps for
  tenant B; cap 3 → `202,202,202,429` with Retry-After, other tenant unaffected;
  **8 racing threads → exactly 3 rows in the database**; the rate window opens
  again after 60 fake seconds.
- Live against uvicorn (local Postgres): 401 / 401; fresh key `202 0.294s`
  (bcrypt) then `202 0.007s` (cache); env key still 202 as the dev tenant; the
  new tenant gets **404** for the dev tenant's run id.

## 7. Questions for Sakshi Roy

1. Why does the key have a *prefix* at all — why not look up by the hash of the
   whole key?
2. Why bcrypt and not SHA-256 for the stored hash, when we happily use SHA-256
   for the cache?
3. Why does the concurrency cap live in Postgres but the request rate limit in
   process memory?
4. Two requests race at cap−1. Walk through why exactly one gets 202.
5. Why 404 and not 403 when tenant B asks for tenant A's run?
6. What exactly does an attacker get from a stolen `api_keys` table?
7. Why does revocation take up to 60 s, and what would fixing that cost?

<details>
<summary>Answers</summary>

1. bcrypt hashes can't be looked up — each has a random salt, so you can't
   compute "the" hash of the presented key to search for. You'd have to run
   bcrypt against *every* row. The prefix is the row's address; the secret is
   checked against exactly one hash.
2. SHA-256 is fast — an attacker with the table can try billions of guesses a
   second. bcrypt is slow on purpose (and salted), making offline guessing
   impractical. The cache digest is different: it holds the *full random key*,
   which has far too much entropy to guess, and the cache needs to be fast.
3. The concurrency cap bounds model spend, so it must be exact across every
   process and survive restarts → shared state in Postgres. The rate limit
   protects the API's own CPU; being N× off with N processes degrades
   gracefully and costs no money.
4. Both call `pg_advisory_xact_lock(tenant)`. One gets it; the other blocks.
   The winner counts (under cap), inserts, commits — releasing the lock. The
   loser now acquires it and its count *includes the winner's committed run*:
   cap reached, 429, nothing inserted.
5. A 403 answers "does this run id exist?" — information leakage. 404 makes
   another tenant's data indistinguishable from nothing.
6. Prefixes, names, and bcrypt hashes. No secrets: the hashes are salted and
   slow to brute-force, and the prefix alone fails the check. Rotation is still
   the right response, but nothing is immediately usable.
7. A verified key is cached for 60 s to avoid 0.3 s of bcrypt per request.
   Fixing it means checking `revoked_at` on every request (one indexed read —
   actually affordable) or an invalidation channel (LISTEN/NOTIFY). The 60 s
   window was chosen as the simplest honest version; the LISTEN/NOTIFY upgrade
   is mechanical if it ever matters.
</details>

## 8. Interview lines

- "API keys are prefix-plus-secret: the prefix is an indexed address, the
  secret is bcrypt-checked and never appears in a query, and a verified key is
  cached as a SHA-256 digest for 60 seconds because 300 ms of bcrypt per
  request is a self-inflicted outage — measured: 294 ms cold, 7 ms cached."
- "Tenant scoping lives in a repository layer where tenant_id is a required
  argument — 'forgot the WHERE clause' stops being an expressible bug. Another
  tenant's runs 404, because 403 would leak that the id exists."
- "There are two different limits because they protect different things: the
  per-minute rate limit guards CPU and lives in process memory, but the
  concurrent-runs cap guards model spend, so it's enforced in Postgres under a
  per-tenant advisory lock — 8 racing creates against a cap of 3 produced
  exactly 3 rows."
