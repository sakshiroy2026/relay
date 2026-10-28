"""Real web tools: Tavily search and a pinned-IP page fetcher (TOOLS_PROVIDER=real).

WHAT IT IS  The two external tools, for real. tavily_search asks the Tavily
            search API and maps its answer onto Relay's SearchResult. fetch_page
            reads one public web page, with every SSRF rule from ssrf.py applied
            and the guard's one known gap closed: it connects to the IP address
            check_url APPROVED, never resolving the name a second time, while
            TLS is still verified against the real host name (SNI + hostname
            check). So DNS rebinding can't swap in an internal address between
            the check and the connection.
GOES IN     tavily_search(query, api_key): a query; the key comes from
            settings.tavily_api_key and is sent only in the Authorization
            header. fetch_page(url): an https URL. Both take injectable seams
            (opener / resolve / transport) so tests never touch the network.
COMES OUT   tavily_search -> up to 5 SearchResult. fetch_page -> PageText with
            the page's title and readable text, cut to page_text_max_chars.
TOUCHES     The network only: api.tavily.com (through the environment's proxy
            settings, like any urllib call) and the fetched site (a direct
            connection to the approved IP — deliberately not through a proxy,
            or the proxy would do its own DNS and the pinning would mean
            nothing).
FAILS WHEN  Never with a crash for the web's sake. A page that is missing, too
            exotic or refused -> ToolError (the model reads it, the breaker
            ignores it). A slow, overloaded or unreachable service (timeout,
            429, 5xx, connection refused) -> RetryableError (retried with
            backoff, counted by the breaker). An unsafe URL -> UnsafeURL (the
            tool turns it into a ToolError). The Tavily request/response shape
            follows Tavily's public API as known at build time and is verified
            by the first real call (see learn/REAL_TOOLS.md).
"""

import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from relay.agent.errors import ToolError
from relay.agent.schemas import PageText, SearchResult
from relay.agent.ssrf import (
    MAX_BYTES,
    MAX_REDIRECTS,
    TIMEOUT_SECONDS,
    Resolver,
    UnsafeURL,
    check_url,
    next_hop,
)
from relay.agent.ssrf import _system_resolve as system_resolve
from relay.core.retry import RetryableError

TAVILY_URL = "https://api.tavily.com/search"
SEARCH_RESULTS = 5
SNIPPET_CHARS = 500
USER_AGENT = "RelayBot/0.1 (company research agent)"
READ_CHUNK = 64 * 1024
REDIRECTS = (301, 302, 303, 307, 308)
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


# ── search ───────────────────────────────────────────────────────────────
def tavily_search(
    query: str,
    api_key: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> list[SearchResult]:
    body = json.dumps(
        {"query": query, "max_results": SEARCH_RESULTS, "search_depth": "basic"}
    ).encode()
    request = urllib.request.Request(
        TAVILY_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:  # before URLError: it's a subclass
        if exc.code == 429 or exc.code >= 500:
            raise RetryableError(f"web_search: search provider answered {exc.code}") from exc
        if exc.code in (401, 403):
            # an operator problem, not the model's; say so plainly, never echo the key
            raise ToolError(
                "web_search is unavailable (the search provider rejected Relay's API key); "
                "continue with what you have, or use flag_for_review"
            ) from exc
        raise ToolError(f"web_search failed: the search provider answered {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RetryableError(f"web_search: {type(exc).__name__} talking to the provider") from exc

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RetryableError("web_search: the provider sent a garbled answer") from exc

    results: list[SearchResult] = []
    for item in (data.get("results") or [])[:SEARCH_RESULTS]:
        try:
            results.append(
                SearchResult(
                    title=str(item.get("title") or item["url"])[:300],
                    url=item["url"],
                    snippet=" ".join(str(item.get("content", "")).split())[:SNIPPET_CHARS],
                )
            )
        except (KeyError, TypeError, AttributeError, ValidationError):
            continue  # one malformed hit doesn't spoil the rest
    return results


# ── fetch: transport ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # names lowercased
    body: bytes = b""
    truncated: bool = False  # the page was longer than MAX_BYTES; we stopped reading


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a fixed IP, while TLS verifies the certificate for the real host name."""

    def __init__(
        self, ip: str, host: str, port: int, timeout: float, context: ssl.SSLContext
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        # server_hostname drives SNI AND the hostname check: the certificate must
        # be valid for the name the URL said, whichever IP we connected to
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


Transport = Callable[[str, str, str, float], RawResponse]  # (ip, host, path?query, timeout)


def https_get_pinned(
    ip: str,
    host: str,
    target: str,
    timeout: float,
    context: ssl.SSLContext | None = None,
    port: int = 443,
) -> RawResponse:
    deadline = time.monotonic() + timeout
    conn = _PinnedHTTPSConnection(ip, host, port, timeout, context or ssl.create_default_context())
    try:
        conn.request(
            "GET",
            target,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
                "Accept-Encoding": "identity",  # no compression: no decompression bombs
            },
        )
        resp = conn.getresponse()
        chunks: list[bytes] = []
        size, truncated = 0, False
        while True:
            if time.monotonic() > deadline:  # a site dripping bytes can't hold the lease
                raise TimeoutError("page download exceeded its time budget")
            chunk = resp.read(READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                truncated = True
                break
        return RawResponse(
            status=resp.status,
            headers={k.lower(): v for k, v in resp.getheaders()},
            body=b"".join(chunks)[:MAX_BYTES],
            truncated=truncated,
        )
    finally:
        conn.close()


# ── fetch: HTML -> readable text ─────────────────────────────────────────
_SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "head"}
_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in _SKIP:
            self._skip_depth += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """(title, readable text): scripts/styles dropped, whitespace collapsed per line."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return " ".join(parser.title.split()), "\n".join(line for line in lines if line)


def _clip(text: str, max_chars: int, truncated: bool) -> str:
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + " … [page truncated]"
    return text + (" … [page truncated]" if truncated else "")


# ── fetch: the tool ──────────────────────────────────────────────────────
def _first_answer(
    ips: list[str], host: str, target: str, timeout: float, transport: Transport
) -> RawResponse:
    last: Exception | None = None
    for ip in ips:  # every one of these passed check_url
        try:
            return transport(ip, host, target, timeout)
        except ssl.SSLCertVerificationError as exc:  # before OSError: it's a subclass
            raise ToolError(
                f"could not fetch https://{host}: its TLS certificate is invalid"
            ) from exc
        except TimeoutError as exc:
            raise RetryableError(f"fetch_page: {host} timed out") from exc
        except (OSError, http.client.HTTPException) as exc:
            last = exc  # refused/reset on this address: try the next approved one
    raise RetryableError(f"fetch_page: could not connect to {host}") from last


def fetch_page(
    url: str,
    max_chars: int,
    resolve: Resolver = system_resolve,
    transport: Transport = https_get_pinned,
) -> PageText:
    deadline = time.monotonic() + TIMEOUT_SECONDS  # for the whole chain, redirects included
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        ips = check_url(current, resolve)  # raises UnsafeURL; these are the ONLY IPs we use
        parts = urlsplit(current)
        host = parts.hostname or ""
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RetryableError(f"fetch_page: {url} took longer than {TIMEOUT_SECONDS} s")
        resp = _first_answer(ips, host, target, remaining, transport)

        if resp.status in REDIRECTS:
            location = resp.headers.get("location")
            if not location:
                raise ToolError(f"could not fetch {current}: redirect without a Location")
            current = next_hop(current, location, hop + 1, resolve)  # re-checked, max 3
            continue
        break
    else:  # pragma: no cover - next_hop raises before the loop can run out
        raise UnsafeURL(f"blocked: more than {MAX_REDIRECTS} redirects")

    if resp.status in (404, 410):
        raise ToolError(f"could not fetch {current}: {resp.status} not found")
    if resp.status == 429 or resp.status >= 500:
        raise RetryableError(f"fetch_page: {host} answered {resp.status}")
    if not 200 <= resp.status < 300:
        raise ToolError(f"could not fetch {current}: the site answered {resp.status}")

    encoding = resp.headers.get("content-encoding", "identity").lower()
    if encoding not in ("identity", ""):
        raise ToolError(f"could not fetch {current}: compressed response ({encoding}) refused")
    ctype = resp.headers.get("content-type", "text/html").lower()
    mime = ctype.split(";")[0].strip()
    if mime not in TEXT_TYPES:
        raise ToolError(f"could not fetch {current}: not a text page ({mime})")

    charset = "utf-8"
    if "charset=" in ctype:
        charset = ctype.split("charset=", 1)[1].split(";")[0].strip().strip('"') or "utf-8"
    try:
        decoded = resp.body.decode(charset, errors="replace")
    except LookupError:  # an unknown charset name
        decoded = resp.body.decode("utf-8", errors="replace")

    if mime == "text/plain":
        title, text = (
            "",
            "\n".join(" ".join(ln.split()) for ln in decoded.splitlines() if ln.strip()),
        )
    else:
        title, text = html_to_text(decoded)
    return PageText(
        url=current,  # type: ignore[arg-type]  # pydantic validates the str into HttpUrl
        title=title or None,
        text=_clip(text, max_chars, resp.truncated),
    )
