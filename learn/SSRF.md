# SSRF guard — the fetch tool may only reach the public internet

## 1. The picture

`fetch_page` fetches a URL that the **model** chose, and the model chooses it
after reading web pages. So a web page can, in effect, tell the server what to
fetch. If a page says "now read `http://169.254.169.254/latest/meta-data/`", a
naive fetcher on a cloud server would read its own cloud credentials. That's
**SSRF** (server-side request forgery): someone outside makes your server send
requests to places only it can reach.

**Analogy:** a receptionist who runs errands for visitors. She'll post a letter
to any public address, but she never takes a visitor's note into the server room,
even if the note is very convincing.

## 2. Runtime story

| Step | What happens |
|---|---|
| the model asks `fetch_page("https://news.example/x")` | `dispatch_tool` validates the args, then calls the fetch tool |
| `check_url` | https? no `user:pw@`? not `localhost`/`*.internal`? |
| resolve the name (real fetcher) | `news.example` → its IP addresses |
| every address must be public | any private, loopback, link-local (incl. `169.254.169.254`), CGNAT or reserved answer → blocked |
| redirect (real fetcher) | `next_hop` re-runs the same checks on the new URL; more than 3 hops → blocked |
| blocked | `UnsafeURL` → `ToolError` → the model gets `ok=False "blocked …"`; the run continues |

Today the fetch tool is still fake (`.example` pages), so it runs the checks
that need no DNS. The real fetcher (demo time) must call `check_url` with DNS,
`next_hop` on every redirect, and enforce `MAX_BYTES` (2 MB) and
`TIMEOUT_SECONDS` (10 s).

## 3. Design decisions

| Choice | Alternative | Why |
|---|---|---|
| Check the IPs **after** DNS resolution | Check the URL text | `https://my-site.example` can resolve to `127.0.0.1`; a text check would let it through |
| Block if **any** resolved address is private | Block only if all are | The attacker controls which answer gets used |
| Use Python's `ipaddress.is_global` | A hand-written list of ranges | It covers RFC1918, loopback, link-local, CGNAT, reserved and more, and is maintained |
| Unwrap IPv4-mapped IPv6 (`::ffff:127.0.0.1`) | Trust the IPv6 checks | A classic bypass: the mapped form is really the IPv4 address |
| https only; no credentials in URLs | Allow http | Blueprint rule; also blocks `file://`, `ftp://` and http downgrades on redirect |
| DNS failure → blocked | Try anyway | Fail closed, like the API key check |
| `check_url` returns the IPs | Return nothing | The real fetcher should connect to a checked IP instead of resolving again (see gap below) |
| Blocked → an `ok=False` tool result | Crash the run | It's the model's mistake (or a page's trick), not a bug: the model reads why and moves on |

**Known gap (honest):** between the check and the real connection, DNS could
answer differently (DNS rebinding). The fix is to connect to an IP that
`check_url` returned. That's a job for the real fetcher at demo time; it's
written in `ssrf.py`'s contract.

## 4. The proof

- `tests/test_ssrf.py`: 24 cases with a stub DNS, no network. Allowed: a public
  https URL. Blocked: http, `file://`, `ftp://`, `user:pw@`, `localhost`,
  `*.internal`, 127/8, 10/8, 172.16/12, 192.168/16, `169.254.169.254`, `::1`,
  `::ffff:127.0.0.1`, `0.0.0.0`, CGNAT, names resolving to those, a name with one
  public and one private answer, NXDOMAIN, a redirect to the metadata IP, an
  https → http redirect, and a 4th redirect. All 36 tests in `tests/` pass.
- `python -m scripts.tools_smoke`: the fake `https://acmepay.example/about` is
  still fetched; `http://169.254.169.254/latest/meta-data/` comes back
  `ok=False "blocked … only https URLs may be fetched"`.

## 5. Questions for Sakshi Roy

1. Why is fetching a URL the model picked dangerous at all?
2. Why check the IP after DNS instead of checking the URL text?
3. A name resolves to one public and one private IP. Why block it?
4. Why must every redirect be checked again?
5. What is DNS rebinding, and how will the real fetcher avoid it?

<details>
<summary>Answers</summary>

1. The model picks URLs after reading untrusted pages, so a page can steer the
   server into fetching internal addresses (cloud metadata, admin panels, the
   database) that outsiders can't reach directly.
2. Any name can point anywhere: `evil.example` can resolve to `127.0.0.1`. Only
   the resolved address says where the request would really go.
3. The attacker controls the DNS answers and which one the client uses. One
   private answer is enough to reach inside.
4. A public page can answer "302 → http://169.254.169.254/". Checking only the
   first URL would follow it straight in.
5. The DNS answer changes between the check and the connection (public first,
   private second). The real fetcher connects to an IP that `check_url` already
   approved instead of resolving again.

</details>

## 6. Interview lines

- "`fetch_page` is guarded against SSRF: https only, and every resolved IP must be
  publicly routable, so the cloud metadata endpoint and internal ranges are
  unreachable even through a redirect."
- "I check addresses after DNS resolution, block the name if any answer is
  private, and unwrap IPv4-mapped IPv6, which is a common bypass."
- "The remaining gap is DNS rebinding; the real fetcher closes it by connecting to
  the IP the guard approved."
