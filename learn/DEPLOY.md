# Deploy — Relay live on Oracle Cloud's free tier

## 1. The picture

Relay goes on one small always-free server: Caddy (HTTPS) in front of the API,
three workers behind it, and Postgres on Neon as before. A public link runs the
**free scripted model** for every visitor (the `demo` tenant). Real-model runs
stay private: only a tenant you switch to `live` can spend money, and only if
you put an `ANTHROPIC_API_KEY` on the server at all.

```
 visitor ──https──▶ Caddy ──▶ API ──▶ Neon Postgres ◀── worker ×3
                    (TLS)    (demo key offered       (claim, replay,
                              only if its tenant      kill one live:
                              is demo = free)         the others resume)
```

**Honest status:** the files below (`docker-compose.prod.yml`,
`deploy/Caddyfile`, the `Dockerfile` additions, `.env.example`) were written and
reviewed but **not built**: the development container has no Docker. The first
`docker compose up --build` on the server is the first build; expect to fix
something small. Oracle's menus are described as of the build date, so names may
differ slightly.

## 2. What you need (all free)

| Thing | Where | Notes |
|---|---|---|
| Oracle Cloud account | oracle.com/cloud/free | asks for a card to verify identity; Always Free resources aren't charged. Pick a home region near you (it can't be changed later) |
| A free subdomain | duckdns.org | sign in, create e.g. `relay-sakshi` → `relay-sakshi.duckdns.org` |
| Neon | you already have it | the same database: the migrations below bring it up to date |
| (later, optional) keys | console.anthropic.com, tavily.com | only for your private live tenant |

## 3. Step by step

**1. Create the server.** Oracle Console → Compute → Instances → Create instance.
- Image: **Ubuntu 24.04** (the full image, not "Minimal").
- Shape: **Ampere (VM.Standard.A1.Flex)**, 2 OCPUs and 12 GB is plenty (the free
  allowance is up to 4 OCPUs / 24 GB in total). If it says *out of capacity*, try
  another availability domain or try again later; it's common.
- Networking: keep "assign a public IPv4 address" on. Add your SSH public key.
- Create it, then note its **public IP**.

**2. Open ports 80 and 443 in Oracle's firewall.** Instance → its subnet → Security
List → Add Ingress Rules: source `0.0.0.0/0`, TCP, destination ports `80` and `443`.

**3. Open them on the server too** (Oracle's Ubuntu images ship their own iptables
rules, and this step is the classic reason "the site doesn't load"):
```bash
ssh ubuntu@<public IP>
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

**4. Point the subdomain at the server.** duckdns.org → your subdomain → set the IP
to the server's public IP → update.

**5. Install Docker and get the code:**
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu && exit        # log out and back in for the group to apply
ssh ubuntu@<public IP>
git clone https://github.com/sakshiroy2026/relay.git && cd relay
```

**6. Write `.env`** (secrets live only here, on the server):
```bash
cp .env.example .env && nano .env && chmod 600 .env
```
Fill in `DATABASE_URL` (your Neon string), `DEV_TENANT_ID`, `RELAY_DOMAIN`
(`relay-sakshi.duckdns.org`). Leave `ANTHROPIC_API_KEY` **empty** for now: the
server then cannot spend money at all.

**7. Build, migrate, start:**
```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml run --rm api alembic upgrade head
docker compose -f docker-compose.prod.yml up -d
```
The migration brings Neon from 003 to 008 (repair/DLQ kinds, tenant limits, the
SSE trigger, idempotency keys, run modes). All are additive.

**8. Create the public demo tenant and its key:**
```bash
docker compose -f docker-compose.prod.yml run --rm api python -m scripts.seed --tenant demo --mode demo
```
Copy the printed `RELAY_API_KEY=rly_...` value into `.env` as `PUBLIC_DEMO_KEY=`, then
`docker compose -f docker-compose.prod.yml up -d api` to reload the API. The page now offers that
key to visitors. The API refuses to offer it if that tenant is ever switched to `live`.

**9. Check it:**
- `https://relay-sakshi.duckdns.org/healthz` → `{"status":"ok"}` (the padlock means
  Caddy got the certificate; the first time can take a minute)
- `https://relay-sakshi.duckdns.org/` → start a run with no key of your own
- `https://relay-sakshi.duckdns.org/ops` → the dashboard
- the kill-and-resume demo, on the server: `docker ps` (find a worker), start a run, then
  `docker kill <that worker's container>` and watch the page's takeover banner. Bring the
  killed worker back with `docker compose -f docker-compose.prod.yml up -d` (whether Docker
  restarts a *killed* container by itself is untested here, so don't rely on it).

**10. Later, real runs (private):** put `ANTHROPIC_API_KEY` (and `TAVILY_API_KEY`) in `.env`,
`up -d` again, create your own tenant with
`docker compose -f docker-compose.prod.yml run --rm api python -m scripts.seed --tenant me --mode live`,
and use **that** key yourself. Never set `PUBLIC_DEMO_KEY` to a live tenant's key (the API would
refuse to offer it anyway).

## 4. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| One always-free VM with Docker Compose | A free PaaS tier | Relay's workers must run all the time; free PaaS tiers sleep or don't allow background workers. A VM also lets the kill-a-worker demo happen live |
| Caddy | nginx + certbot | Automatic HTTPS from one line of config, and `flush_interval -1` keeps SSE streaming |
| Neon stays the database | Postgres on the VM | Already set up, backed up and free; the VM stays disposable |
| Public = the free fake model via a published demo key | Real model for visitors | A public link that spends money is a bill anyone can run up |
| `/demo-config` re-checks the tenant's mode on every request | Trust the env var | A typo can't publish a paid key: a live tenant's key is never offered |
| `.env` only on the server, `chmod 600` | Secrets in compose files | Nothing secret ever enters git; `.env.example` documents the names |

## 5. What can go wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| browser can't connect at all | iptables (step 3) or the Security List (step 2) | redo both; `sudo iptables -L INPUT -n` should show 80/443 ACCEPT |
| Caddy logs "challenge failed" | DuckDNS IP wrong, or port 80 closed | fix the IP; Let's Encrypt needs port 80 reachable |
| API restarts in a loop | `.env` value missing (`DATABASE_URL`, `DEV_TENANT_ID`) | `docker compose -f docker-compose.prod.yml logs api` |
| workers log "preflight: ANTHROPIC_API_KEY not set" | expected while the server is $0-only | nothing to do; demo runs don't need it |
| "out of capacity" creating the VM | free ARM capacity is busy in that region | another availability domain, or retry later |

## 6. Questions for Sakshi Roy

1. Why can't a visitor of the public page spend your Anthropic credits?
2. Why does killing a worker on the server not break the demo?
3. Why do the iptables rules matter on Oracle when the Security List already allows 80/443?
4. Why keep Neon instead of running Postgres on the VM?

<details>
<summary>Answers</summary>

1. Visitors get the demo tenant's key, and demo runs use the scripted fake model
   ($0). The API checks that tenant's mode on every request and never offers a
   live tenant's key. With `ANTHROPIC_API_KEY` empty, even a live tenant's run
   would fail with $0 spent.
2. The lease expires, another worker claims the run and replays the ledger, the
   same as the 200-run chaos harness. `up -d` brings the killed container back.
3. There are two firewalls: Oracle's network (the Security List) and the server's
   own iptables, which Oracle's Ubuntu image ships with deny rules. Both must allow
   the ports.
4. It's already provisioned, backed up and free, and it keeps the VM disposable:
   rebuild the server and nothing is lost.
</details>
