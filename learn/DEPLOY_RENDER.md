# Deploy — Relay live on Render's free plan

## 1. The picture

The Oracle plan (learn/DEPLOY.md) needed a credit card for identity
verification; Sakshi doesn't have one, so Relay runs on Render's free plan
instead: **one container** holding the API and the workers, built by Render
from the GitHub repo, with Neon as the database exactly as before. HTTPS and
the public URL come from Render, so Caddy and DuckDNS fall away.

```
 visitor ──https──▶ Render (one free web service)
                    ├── uvicorn API :$PORT   ──▶ Neon Postgres
                    └── worker ×2 (same container) ◀──┘
                        (claim, replay; a dead worker is
                         restarted by start.sh in 2 s)
```

Think of it as the same restaurant squeezed into a food truck: the same
kitchen (API) and the same cooks (workers), just sharing one vehicle because
the free lot only parks one.

## 2. Files

| File | What it is |
|---|---|
| `deploy/single/Dockerfile` | one image: API + workers; non-root user, port from `$PORT` |
| `deploy/single/start.sh` | migrate → start N workers (restart loop) → exec uvicorn |
| `docker-compose.prod.yml`, `deploy/Caddyfile` | the Oracle/VM path, kept for when a real VM shows up |

## 3. What was actually verified (on the Windows host, 29 Oct 2026)

Render's free plan gives 512 MB RAM and ~0.1 CPU, so the image was run
locally with exactly those limits before any sign-up:

- `docker run --memory=512m --cpus=0.1` against real Neon: boots in ~2.5 min
  (CPU-starved pip/Python imports), settles at **120 MB** with API + 2 workers.
- A demo run (`github.com`) succeeded in ~12 s under those limits.
- The full CI suite (ruff, mypy, pytest, eval gate) run in a clean
  `python:3.13-slim` container against a throwaway Postgres 16.
- `/demo-config` returns `{"demo_key": null}` until `PUBLIC_DEMO_KEY` is set,
  and the mode re-check means a live tenant's key is never offered.

### Live on Render (29 Oct 2026)

- URL: https://relay-sakshi.onrender.com (service `relay-sakshi`, Singapore,
  Free plan, Dockerfile `./deploy/single/Dockerfile`, health check `/healthz`).
- **Branch: check it.** The service was created on `deploy-render`. That branch
  still exists and, as of 29 Oct, points at the same commit as `main`
  (`36605ba`) — so the live site looks correct while being one merge behind
  everything that lands on `main` from now on. Either point Render's
  *Settings → Build & Deploy → Branch* at **`main`** (preferred: that is where
  PRs merge), or remember to merge into `deploy-render` too, every time.
- First deploy of `bc1a26a`: `/healthz` and `/readyz` OK, `/demo-config`
  offers the demo key, `/` and `/ops` return 200.
- Public end-to-end run `b6e803fc` (anthropic.com, demo key fetched from
  `/demo-config` like a visitor) succeeded in ~4 s. $0 spent: no
  `ANTHROPIC_API_KEY` on the service.

## 3b. Environment variables on the service

| Variable | Value | Why |
|---|---|---|
| `DATABASE_URL` | the Neon connection string | already set |
| `DEV_TENANT_ID`, `PUBLIC_DEMO_KEY` | the demo tenant and its key | already set |
| `LEASE_SECONDS` | `15` | a kill is watchable in under a minute |
| `FAKE_DELAY_SECONDS` | `2.5` | demo runs stream instead of looking pre-rendered |
| `GEMINI_API_KEY` | the free-tier key | already set; never printed, never in a URL |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | **add** — the config default, pinned so a default change can't move the live model silently |
| `PUBLIC_REAL_RUNS_PER_DAY` | `30` | **add** — the shared daily allowance; `0` turns the toggle off everywhere |

`GEMINI_MODEL` can be switched to `gemini-3.1-flash-lite` or
`gemini-3.8-flash` without a code change — both are priced in
`relay/llm/pricing.py`. Check the free-tier limits first in AI Studio →
*View your active rate limits* (Google no longer publishes them per model).

The service needs **no** `ANTHROPIC_API_KEY`: the Claude adapter is unused.

## 4. Render specifics that bit

| Thing | Detail |
|---|---|
| Spin-down | 15 min idle → sleeps; next visit waits ~1 min behind Render's loading page. Fine for a resume link; stated honestly in the README |
| `$PORT` | Render injects it; start.sh passes it to uvicorn (default 10000) |
| Workers on the free plan | Not allowed as separate services → they live in the API's container, restarted by start.sh. The lease/claim design doesn't care where a worker runs |
| 750 free instance-hours/month | One service sleeping when idle stays well under it |
| Ephemeral filesystem | Irrelevant: all state is in Neon |
| Docker DNS on the Windows host | Local tests needed `--dns 1.1.1.1`; Neon's pooler host resolved to unreachable IPv6 first. A host quirk, not a Render one |

## 5. Design decisions

| Decision | Alternative | Why |
|---|---|---|
| One container, workers forked by start.sh | Separate worker services | Free plan has no background workers; the ledger makes worker location irrelevant |
| 2 workers (not 3) | 3 as on the VM plan | 512 MB budget; 2 still demonstrates takeover (kill one, the other resumes) |
| Keep the Oracle files | Delete them | The VM path is better (always-on, live kill demo); it waits for a card |
| `FAKE_DELAY_SECONDS=0.5` baked into the image | Instant steps | Visitors should *see* steps streaming; instant runs look pre-rendered |

## 6. What can go wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| First visit hangs ~1 min | Free-plan wake-up | Expected; the loading page is Render's |
| Build fails on push | Dockerfile path not set to `deploy/single/Dockerfile` | Service → Settings → Build & Deploy |
| API up but runs stay pending | Workers died (check logs for `worker N exited`) | `DATABASE_URL`/`DEV_TENANT_ID` env vars; logs say why |
| `/demo-config` gives null | `PUBLIC_DEMO_KEY` unset, or its tenant isn't demo-mode | Seed a demo tenant, paste its key into the env var |
| No "Run with real AI" toggle on the page | `/demo-config` says `real_ai: false`: `GEMINI_API_KEY` missing on the service, or `PUBLIC_REAL_RUNS_PER_DAY=0` | Set both, redeploy |
| Real-AI runs fail with `model_fatal: daily quota exhausted` | the free tier's per-day quota is spent (it is per **project**, not per key) | Wait for midnight US Pacific, or lower `PUBLIC_REAL_RUNS_PER_DAY` so Relay's cap bites first |
| Real-AI runs fail with `model_misconfigured` | `GEMINI_API_KEY` not visible to the workers | It's a service-level env var, so a redeploy is needed after adding it |
| A visitor sees "waiting for the one real-AI slot" for a long time | another visitor's real-AI run is in flight, or one is stuck | Expected while one runs; a stuck run stops holding the slot after 5 minutes |
