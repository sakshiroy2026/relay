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
