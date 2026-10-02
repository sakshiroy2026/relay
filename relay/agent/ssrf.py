"""SSRF guard for fetch_page: only public https URLs, checked after DNS resolution.

WHAT IT IS  The egress rule for the one tool that reaches out to URLs the model
            (or a web page it read) chose. Without it, a page saying "now fetch
            http://169.254.169.254/..." could make the server read its own cloud
            credentials or internal services.
GOES IN     check_url(url, resolve=...): a URL. With resolve=None no DNS lookup is
            done (fake tools: .example hosts don't resolve); the real fetcher
            passes socket.getaddrinfo (the default) or a stub in tests.
            next_hop(current_url, location, hops): a redirect to validate.
COMES OUT   check_url: the list of resolved IP strings, all public (the real
            fetcher should connect to one of these, not resolve again).
            next_hop: the absolute URL of the redirect target, already checked.
TOUCHES     DNS only (when resolving). No network request is made here.
FAILS WHEN  Anything unsafe -> UnsafeURL (the tool turns it into an ok=False
            result the model reads). DNS failure -> UnsafeURL too: fail closed.

Rules (blueprint §10): https only; no user:password@ in URLs; every resolved
address must be globally routable (rejects RFC1918, loopback, link-local incl.
the 169.254.169.254 metadata endpoint, CGNAT 100.64/10, unspecified, multicast,
reserved, and IPv4-mapped IPv6 forms of those); at most MAX_REDIRECTS hops, each
re-checked. The real fetcher must also enforce MAX_BYTES and TIMEOUT_SECONDS.
"""

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

MAX_REDIRECTS = 3
MAX_BYTES = 2 * 1024 * 1024  # 2 MB response cap, for the real fetcher
TIMEOUT_SECONDS = 10  # well below the lease, so a slow site can't cost a run its lease

Resolver = Callable[[str], list[str]]


class UnsafeURL(Exception):
    """The URL may not be fetched. The message is safe to show to the model."""


def _system_resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in infos})


def _is_public(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text.split("%")[0])  # drop an IPv6 zone id like fe80::1%eth0
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is 127.0.0.1
    return ip.is_global and not ip.is_multicast


def check_url(url: str, resolve: Resolver | None = _system_resolve) -> list[str]:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UnsafeURL(f"blocked {url!r}: only https URLs may be fetched")
    if parts.username or parts.password:
        raise UnsafeURL(f"blocked {url!r}: credentials in URLs are not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURL(f"blocked {url!r}: no host")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".internal"):
        raise UnsafeURL(f"blocked {url!r}: internal host name")

    try:
        literal: list[str] | None = [str(ipaddress.ip_address(host))]
    except ValueError:
        literal = None  # a name, not an IP literal

    if literal is not None:
        addresses = literal
    elif resolve is None:
        return []  # fake mode: name-only checks
    else:
        try:
            addresses = resolve(host)
        except OSError as exc:
            raise UnsafeURL(f"blocked {url!r}: could not resolve {host}") from exc
        if not addresses:
            raise UnsafeURL(f"blocked {url!r}: {host} resolved to nothing")

    bad = [a for a in addresses if not _is_public(a)]
    if bad:
        # one private answer is enough: an attacker controls which one gets used
        raise UnsafeURL(f"blocked {url!r}: {host} resolves to a non-public address")
    return addresses


def next_hop(
    current_url: str, location: str, hops: int, resolve: Resolver | None = _system_resolve
) -> str:
    """Validate redirect number `hops` (1-based) from current_url to `location`."""
    if hops > MAX_REDIRECTS:
        raise UnsafeURL(f"blocked: more than {MAX_REDIRECTS} redirects")
    target = urljoin(current_url, location)
    check_url(target, resolve)
    return target
