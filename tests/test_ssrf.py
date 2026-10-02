"""Unit tests for the SSRF guard. DNS is a stub dict: no network."""

import pytest

from relay.agent.ssrf import MAX_REDIRECTS, UnsafeURL, check_url, next_hop

DNS = {
    "public.example": ["93.184.216.34"],
    "rebind.example": ["93.184.216.34", "10.0.0.5"],  # one public, one private answer
    "metadata.example": ["169.254.169.254"],
    "v6-loopback.example": ["::1"],
    "mapped.example": ["::ffff:127.0.0.1"],
    "cgnat.example": ["100.64.0.1"],
}


def stub_resolve(host: str) -> list[str]:
    if host not in DNS:
        raise OSError("NXDOMAIN")
    return DNS[host]


def test_public_https_url_passes_and_returns_ips() -> None:
    assert check_url("https://public.example/about", stub_resolve) == ["93.184.216.34"]


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example/",  # not https
        "file:///etc/passwd",
        "ftp://public.example/",
        "https://user:pw@public.example/",  # credentials in the URL
        "https://localhost/admin",
        "https://db.internal/",
        "https://127.0.0.1/",
        "https://10.1.2.3/",
        "https://192.168.0.1/",
        "https://172.16.5.4/",
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
        "https://[::1]/",
        "https://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6 loopback
        "https://0.0.0.0/",
        "https://metadata.example/",  # a name that resolves to the metadata IP
        "https://v6-loopback.example/",
        "https://mapped.example/",
        "https://cgnat.example/",
        "https://rebind.example/",  # any private answer blocks the whole name
        "https://nxdomain.example/",  # DNS failure fails closed
    ],
)
def test_unsafe_urls_are_blocked(url: str) -> None:
    with pytest.raises(UnsafeURL):
        check_url(url, stub_resolve)


def test_fake_mode_skips_dns_but_still_checks_literals() -> None:
    assert check_url("https://acmepay.example/about", resolve=None) == []
    with pytest.raises(UnsafeURL):
        check_url("https://169.254.169.254/", resolve=None)
    with pytest.raises(UnsafeURL):
        check_url("http://acmepay.example/about", resolve=None)


def test_redirect_is_rechecked_and_relative_locations_resolve() -> None:
    assert next_hop("https://public.example/a", "/b", 1, stub_resolve) == "https://public.example/b"
    with pytest.raises(UnsafeURL):
        next_hop("https://public.example/a", "https://169.254.169.254/", 1, stub_resolve)
    with pytest.raises(UnsafeURL):  # a downgrade to http is blocked too
        next_hop("https://public.example/a", "http://public.example/b", 1, stub_resolve)


def test_redirect_chain_is_capped() -> None:
    next_hop("https://public.example/a", "/b", MAX_REDIRECTS, stub_resolve)
    with pytest.raises(UnsafeURL):
        next_hop("https://public.example/a", "/b", MAX_REDIRECTS + 1, stub_resolve)
