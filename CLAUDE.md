# Relay — instructions for Claude Code (handoff, 2026-10-28)

Relay is Sakshi Roy's resume project: a durable, crash-safe runtime for LLM agents
(Python 3.13, FastAPI, raw psycopg 3, Postgres on Neon, Docker Compose workers).
Read `README.md` (Status, Measured results, Roadmap), `NOTES.md` and the newest
`learn/*.md` before changing anything. Planning docs (`_handoff/`) are local-only.

## Ground rules (non-negotiable)

- **Money.** The next phase spends real money. Before ANY command that calls a paid
  API, state the estimated cost and wait for Sakshi's explicit "go". Never run a paid
  harness in a loop or in the background without that go. Default model for all
  development: `claude-haiku-5-5` ($0.10 / $0.50 per MTok). Never switch to a more
  expensive model on your own; Sonnet 5.5 is only for the repair ladder's rung 3 and
  the eval judge, and only after asking.
- **Secrets.** Never print, `cat` or commit `.env`; never ask for a key in chat;
  never write a key into notes, tests or HTML. Scan output for `sk-ant-`, `rly_`,
  `tvly-` and the database URL before pasting it anywhere. `.env` is gitignored.
- **Don't touch proven code** unless the stage needs it: migration 001, the claim
  query, fencing in `append_step`, the guarded `mark_succeeded` / `mark_failed`.
- **House style.** Every new or changed module gets the docstring contract
  (WHAT IT IS / GOES IN / COMES OUT / TOUCHES / FAILS WHEN). Raw SQL, no ORM,
  no new frameworks; a new dependency needs a reason in the notes.
- **Before every commit:** `ruff format .` → `ruff check .` →
  `python -m mypy --explicit-package-bases relay/ chaos/ scripts/ tests/ evals/` →
  `python -m pytest tests/ -q` → `git status` (check `.env` isn't staged).
- **Honest records.** README claims only what was verified, with real run ids.
  Label where numbers were measured (Neon vs local Postgres in a cloud container).
- **One stage at a time:** build → prove with tests → commit → push → write notes
  (`learn/DAYn.md` in the DAY8/DAY16 style: picture + analogy, file table, runtime
  story, decisions, crash walk, proofs, questions with collapsed answers, interview
  lines; NOTES.md entry "built by Claude Code … Explain-back owed") → report in a few
  lines → wait for the go.

## Where things stand

`main` has everything up to PR #2 (Days 16, 9, 10, 17, 11, the client
Idempotency-Key, real tools). Branch `paid-phase` (PR #3) adds, all built and
tested for **$0** (a mock HTTP transport drives the real SDK; nothing paid
has run yet): P1 the real model adapter, P5 rung 3 + fallback, P6 the eval
harness + GitHub Actions CI (passing), and the deployment config + guide
(`learn/DEPLOY.md`, Oracle Cloud Always Free; never built: no Docker in the
dev container). 200 tests pass. Runs are `demo` (fake model + fake tools, $0)
unless their tenant's `run_mode` is `live` (migration 008).

Local environment in a cloud container (no Docker, no Neon): Postgres 16 must run as
the `postgres` user — `initdb -D /tmp/relay-pg/data -U relay --auth=trust`,
`pg_ctl -D /tmp/relay-pg/data -o "-k /tmp/relay-pg/sock -c listen_addresses=" start`,
`createdb -h /tmp/relay-pg/sock -U relay relay`; then
`DATABASE_URL="postgresql://relay@/relay?host=/tmp/relay-pg/sock"`,
`DEV_TENANT_ID=00000000-0000-0000-0000-000000000001`, `pip install -e ".[dev]"`,
`alembic upgrade head`, insert the dev tenant row, `python -m pytest tests/ -q`.
Workers are plain `python -m relay.worker` processes (`chaos/harness.py`).

## The paid phase, in order (estimates at Haiku 5.5 prices)

A run is ~6–10 model calls, roughly 40k input + 4k output tokens (thinking counts
as output) ≈ **$0.006 per run** on Haiku 5.5 (≈ $0.12 on Sonnet 5.5).

| # | Stage | Builds | Paid runs needed | Est. cost |
|---|---|---|---|---|
| P1 | Real model — **BUILT** (`relay/llm/anthropic_client.py`, learn/P1_REAL_MODEL.md, tested via the SDK + mock transport). Left: the paid proof `python -m scripts.live_smoke --yes-spend --runs 3` on a live tenant (`seed.py --tenant NAME --mode live`) | ~3–10 runs | ~$0.02–0.06 |
| P2 | Real tools — **BUILT** (learn/REAL_TOOLS.md, 32 tests). Left to do: one real Tavily search to verify its request/response format (free tier, $0; the docs host was blocked from the container, so the format is from memory), then one real fetch. Needs `TAVILY_API_KEY` (free signup) and `TOOLS_PROVIDER=real`. The pinned fetcher bypasses HTTP proxies by design, so in a proxied cloud container real fetches may be blocked: verify on a normal host | 1 search + 1 fetch | $0 |
| P3 | Cost hygiene — **BUILT as settings** (`LLM_PROMPT_CACHE=true`, automatic `cache_control`; `LLM_EFFORT=medium`). Left: measure with `scripts/live_smoke.py`: `cache_read_share` > 0 (Haiku has a minimum cacheable prefix, so measure, don't assume), then `LLM_EFFORT=low` vs `medium` on the same domains, cost and success side by side | ~6–10 runs | ~$0.06 |
| P4 | Chaos harness on the real model | re-run `chaos/fault_test.py`-style kill tests, small N (≈30), budget caps on | ~30 runs | ~$0.30 |
| P5 | Rung 3 + fallback — **BUILT** (learn/P5_ESCALATION.md, budget-guarded). Left: one paid run that forces rung 3 (e.g. `FAKE_BROKEN_FINAL` has no effect on the real model, so pick a domain it can't settle and watch for `escalated:` tags) | ~5 runs | ~$0.20 |
| — | Deployment — **config + guide ready** (learn/DEPLOY.md: Oracle Cloud Always Free VM + DuckDNS + Neon, `docker-compose.prod.yml`, Caddy). Never built (no Docker in the dev container): the first build is on the server. Public visitors get a demo-tenant key ($0) | Sakshi follows the guide | $0 |
| — | Day 18 hand-labelling | Sakshi's own work | — | $0 |
| P6 | Eval — **BUILT** (`evals/`, learn/P6_EVAL.md, CI gate). Left: Sakshi's labels in `evals/golden.jsonl` (format: evals/README.md), then `python -m evals.run_eval --mode live --yes-spend`, commit `evals/results.json`, set real floors in `evals/thresholds.json` | ~50 runs + 50 judge calls | ~$0.40 |

Total model spend for P1–P6: about **$2–3**; $5 of credits covers it with margin.
Safety nets, all three on: (1) a Console spend limit, (2) Relay's per-run
`budget_usd` cap (the ledger enforces it), (3) state the estimate and wait for
the go before every paid command.

The client `Idempotency-Key` on `POST /v1/runs` is built (migration 007,
learn/IDEMPOTENCY_KEY.md).

## Key setup (Sakshi does this; Claude never sees the key)

- Windows: one line `ANTHROPIC_API_KEY=...` in `C:\dev\relay\.env` (and later
  `TAVILY_API_KEY=...` + `TOOLS_PROVIDER=real` for real search).
- Cloud sessions: the environment's settings (cloud environment menu in the session
  title bar → Edit) → Network secrets if offered, otherwise an environment variable
  named `ANTHROPIC_API_KEY` (and `TAVILY_API_KEY`; `api.tavily.com` may also
  need allowing in the environment's network settings). A new session picks it up. Check it exists with
  `python -c "import os; print(bool(os.environ.get('ANTHROPIC_API_KEY')))"` — never
  print the value.
