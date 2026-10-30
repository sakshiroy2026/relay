# Real tools — Tavily search and a pinned-IP page fetcher (P2, built for $0)

## 1. The picture

Until now the agent's "internet" was two dictionaries: one search answer and
two pages about one company. Good enough to prove durability, useless for real
research. This stage builds the real pair behind `TOOLS_PROVIDER=real`: Tavily
search (free tier: 1,000 searches a month) and a fetcher for any public web
page. Everything was built and tested **without a key and without touching the
internet**. The tests use a stub DNS, stub HTTP and one local TLS server.

The fetcher is the dangerous one, because the URL comes from the model, and the
model got it from a web page. The SSRF guard (learn/SSRF.md) already decided
what may be fetched, but it was written with one admitted gap: **DNS
rebinding**. The guard looks a name up and approves the answer, but the
connection used to look the name up *again*, and an attacker's DNS could
answer differently the second time ("public" for the check, `127.0.0.1` for
the connection).

**The visitor-pass analogy.** Security checks a visitor's ID at the gate and
writes the room number on the pass. The old way was like letting the visitor
then ask a stranger in the corridor which room to go to. Now the escort walks
them to the room on the pass, and that room's door still checks the name on
their ID.

That's exactly the fix: Relay connects to the **IP the guard approved**, and TLS
still verifies the certificate for the **name in the URL** (SNI plus the
hostname check). A different name served from that IP fails the TLS check.

## 2. Each file's job

| File | One line |
|---|---|
| `relay/agent/real_tools.py` (new) | `tavily_search` (stdlib `urllib`, key only in the `Authorization` header) and `fetch_page` (pinned IP, redirects re-checked, 2 MB cap, 10 s budget for the whole chain, `Accept-Encoding: identity`, text pages only, HTML → readable text cut to `page_text_max_chars`) |
| `relay/agent/tools.py` | `_real_web_search`, `_real_fetch_page`; `TOOLS_PROVIDER=real` builds the registry from them under the **same names, descriptions and input schemas** as the fake pair; refuses to boot without `TAVILY_API_KEY` |
| `relay/agent/errors.py` (new) | `ToolError` moved here (re-exported from tools.py) so real_tools can raise it without a circular import |
| `relay/config.py` | `tools_provider: fake \| real`, `tavily_api_key`, `page_text_max_chars = 8000` (~2k tokens per page, the model's main input cost) |
| `tests/test_real_tools.py` (new) | 32 tests, no network (below) |

No new dependencies: `urllib`, `http.client`, `ssl`, `socket` and `html.parser`
are all in the standard library.

## 3. Runtime story: the model asks for a page that redirects

| Step | What happens |
|---|---|
| model: `fetch_page("https://acme.example/old")` | `dispatch_tool` validates the URL, the breaker allows the call |
| `check_url` | https, no `user:pw@`, the name resolves to `93.184.216.34`, which is public → approved |
| connect | TCP to **93.184.216.34** itself; TLS with `server_hostname="acme.example"`, so the certificate must be valid for that name |
| response `301 Location: /new` | `next_hop` re-runs every check on `https://acme.example/new` (hop 1 of max 3) |
| second request | again to the freshly approved IP, with whatever time is left of the 10 s budget |
| `200 text/html` | read in 64 KB chunks, stop at 2 MB; script/style/head dropped, title kept, whitespace collapsed, cut to 8,000 characters |
| result | `PageText{url, title, text}` → `tool_result` → the model |

Where failures go is the Day 8 split, kept exactly:

| What went wrong | Becomes | So |
|---|---|---|
| 404/410, other 4xx, PDF/image, gzip despite `identity`, bad TLS certificate, 401 from Tavily | `ToolError` | the model reads why; no retry; the breaker isn't touched (the tool is healthy) |
| timeout, connection refused on every approved IP, 429, 5xx, garbled Tavily answer | `RetryableError` | retried with full-jitter backoff; counted by the breaker |
| unsafe URL or redirect | `UnsafeURL` → `ToolError` | blocked, the run continues |

## 4. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| Connect to the approved IP, verify TLS for the name | Resolve again inside the HTTP client | Closes DNS rebinding, the gap the SSRF note promised to close here. Keeping the hostname check means pinning never weakens TLS |
| Standard library only | `httpx` / `requests` | No new dependency for two calls, and `http.client` lets the connection be pinned with a 10-line subclass. In httpx that takes a custom transport |
| The fetcher doesn't use an HTTP proxy | Honour `HTTPS_PROXY` like urllib does | A proxy does its own DNS lookup, which would undo the pinning. Tavily (a fixed, trusted host) does go through the environment's proxy settings |
| `Accept-Encoding: identity`, refuse compressed answers | Decompress | A 2 MB gzip can unpack to gigabytes (a "zip bomb"). Asking for plain bytes keeps the 2 MB cap honest |
| One 10 s budget for the whole redirect chain, checked between chunks | A socket timeout per read | A site dripping one byte every 9 s would pass every per-read timeout and still eat the run's lease |
| Page text capped at 8,000 characters | Send the whole page | Page text is most of what a run pays for. ~2k tokens per page keeps a run near the $0.006 estimate on Haiku 5.5. It's a setting, tuned in P3 |
| The same names and schemas as the fake tools | New tool definitions | The model's menu (and the prompt-cache prefix) doesn't change when the provider does; every fake-mode test still describes the real system |
| No key → refuse to boot | Fail on the first search | Fail before a paid run starts, not halfway through one |

## 5. Crash walk

| Worker dies… | State | Next worker |
|---|---|---|
| mid-download | `tool_call` without `tool_result` | Day 6 edge case 1: re-runs the fetch (read-only, safe to repeat) |
| mid-search | same | re-runs the search: one more free-tier search, nothing else |
| while the site is down for minutes | breaker opens after 5 consecutive failures (Day 8) | the model is told fetch_page is unavailable and adapts |

## 6. The proofs (no network)

`tests/test_real_tools.py`, 32 tests, 148 in total:
- **Pinning:** with a DNS that answers public first and `127.0.0.1` on a second
  lookup, the fetcher looks the name up **once** and connects to the approved IP.
  A deliberate mutant that re-resolves before connecting makes this test fail.
  The mutation was run and reverted to confirm that.
- **Real TLS, locally:** a TLS server on 127.0.0.1 with a certificate for
  `relay-test.example`. Pinned to that IP with the right name: 200, and the
  server saw `Host: relay-test.example`. Same IP, URL claiming `other.example`:
  `SSLCertVerificationError`.
- **Redirects:** relative redirect followed and re-checked; into `10.0.0.7` blocked
  before any connection; down to `http://` blocked; a 4th hop blocked.
- **Buckets:** 404/403/PDF/gzip/bad certificate → `ToolError`; 503/429/timeout/all
  approved IPs refusing → `RetryableError`; a second approved IP is tried when the
  first refuses.
- **Limits:** text cut to the character budget with a marker; a page over 2 MB marked
  truncated.
- **Search:** results mapped, malformed hits skipped, at most 5; the key is in the
  `Authorization` header and **not** in the URL, the body or any error message;
  401/400 → `ToolError`, 429/502/DNS error/timeout/garbled JSON → `RetryableError`.
- **Registry:** `TOOLS_PROVIDER=real` without a key refuses to boot; with a key the
  model sees the identical menu.

**Not yet proven (honest):** the Tavily request and response format. It follows
Tavily's public API as known at build time (`POST https://api.tavily.com/search`,
`Authorization: Bearer`, `query` / `max_results` / `search_depth: basic`,
results with `title` / `url` / `content`). The docs site couldn't be reached
from this container to re-check. The first real search (free tier, $0) in the
paid phase confirms or corrects it. Also, the cloud development container sends
traffic through a proxy, which the pinned fetcher deliberately doesn't use. So
real fetches are expected to work on a normal host (the EC2 deployment, a
laptop), and may be blocked inside such a container.

## 7. Questions to answer yourself

1. What is DNS rebinding, and which single line in `fetch_page` closes it?
2. If Relay connects to an IP, why does the TLS certificate still protect us?
3. Why does the fetcher refuse to go through an HTTP proxy?
4. Why ask for `Accept-Encoding: identity` instead of handling gzip?
5. A site sends one byte every 9 seconds. Which rule stops it, and why isn't the
   socket timeout enough?
6. Why does a 401 from Tavily become a `ToolError` and not a `RetryableError`?
7. Why keep the real tools' names and schemas identical to the fake ones?

<details>
<summary>Answers</summary>

1. The attacker's DNS answers "public" when the guard checks and "internal" when
   the client connects. `resp = _first_answer(ips, ...)` connects to the `ips`
   that `check_url` returned. The name is never looked up a second time.
2. TLS is told the URL's name (`server_hostname`), and the certificate must be
   valid for it. An attacker can point a name at an IP but can't produce a valid
   certificate for a name they don't own on that IP.
3. A proxy resolves the name itself, so the IP Relay checked wouldn't be the IP
   the proxy connects to, and the rebinding gap would reopen.
4. Compression makes the 2 MB cap meaningless: a small gzip body can expand to
   gigabytes. Plain bytes are capped exactly.
5. The 10-second budget for the whole fetch, checked between chunks. A socket
   timeout restarts on every byte, so a slow drip never trips it.
6. Retrying with the same rejected key fails identically. It's an operator
   problem, so the model is told plainly and the run continues; retrying would
   just burn the lease.
7. The model's menu, the system prompt and the prompt-cache prefix stay the same
   across providers. Every test written against the fake tools still describes
   what the real system shows the model.
</details>

## 8. Interview lines

- "The page fetcher closes DNS rebinding: it connects to the IP address the SSRF
  guard approved, never re-resolving, while TLS still verifies the certificate
  for the URL's hostname. I proved it with a DNS stub that changes its answer,
  and a mutant that re-resolves makes that test fail."
- "It's standard library only: a 10-line `HTTPSConnection` subclass pins the IP,
  `identity` encoding keeps the 2 MB cap honest against zip bombs, and one
  10-second budget covers the whole redirect chain, so a slow-drip site can't
  hold a worker's lease."
- "Real and fake tools share names and schemas, so the model's menu and the
  prompt-cache prefix don't change between test and production, and every
  failure maps onto the Day 8 split: the model reads what's its business, the
  retry loop and breaker handle what's transient."

## The first real calls (30 Oct 2026, Windows laptop, not behind a proxy)

Built from memory and stubs until now: the Tavily docs host was blocked where this was
written, so the request and response format had never met the real service.

| Call | Result |
|---|---|
| 1 `tavily_search` | 5 results parsed in 2.1 s, with titles, URLs and 500-character snippets. The format from memory was right; **no code change needed** |
| 1 `fetch_page` (`razorpay.com/blog/...`, from those results) | 0.4 s through the pinned-IP fetcher with real TLS: title extracted, text capped at about 8,000 characters |

**Since then:** 21 `live` runs in the first eval (80 searches, 4 page fetches:
`learn/P6_LIVE_EVAL.md`). One search also doesn't show how often results are
useful: this one returned the same page twice (`http` and `https`), and mostly third-party
sites rather than the company's own. That's something to check when reading the eval's misses.
