"""Real tools (P2) without the internet: stub DNS, stub transports, a local TLS server.

What must hold before a real key ever exists: the fetcher only connects to IPs
the SSRF guard approved (DNS rebinding closed), redirects are re-checked, every
failure lands in the right bucket (ToolError for the model, RetryableError for
the retry loop + breaker), and the search client sends the key only in a
header and survives a malformed answer.
"""

import datetime
import io
import json
import ssl
import threading
import urllib.error
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

import relay.agent.tools as tools_mod
from relay.agent import real_tools
from relay.agent.errors import ToolError
from relay.agent.real_tools import RawResponse, fetch_page, html_to_text, tavily_search
from relay.agent.ssrf import MAX_REDIRECTS, UnsafeURL
from relay.core.retry import RetryableError

DNS = {
    "acme.example": ["93.184.216.34"],
    "two-ips.example": ["93.184.216.40", "93.184.216.41"],
    "other.example": ["93.184.216.50"],
    "evil.example": ["10.0.0.7"],
}

PAGE = (
    "<html><head><title>About Acme</title><style>.x{}</style>"
    "<script>alert('no')</script></head><body><nav>Menu</nav>"
    "<h1>Acme Payments</h1><p>Founded in 2016 in   Bengaluru.</p></body></html>"
)


def resolve(host: str) -> list[str]:
    if host not in DNS:
        raise OSError("NXDOMAIN")
    return DNS[host]


class Recorder:
    """A transport stub: answers from a script, records what it was asked."""

    def __init__(self, *answers: RawResponse | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, ip: str, host: str, target: str, timeout: float) -> RawResponse:
        self.calls.append((ip, host, target))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def html(status: int = 200, body: str = PAGE, **headers: str) -> RawResponse:
    return RawResponse(
        status, {"content-type": "text/html; charset=utf-8", **headers}, body.encode()
    )


# ── HTML -> text ─────────────────────────────────────────────────────────
def test_html_to_text_keeps_the_words_and_drops_the_machinery() -> None:
    title, text = html_to_text(PAGE)
    assert title == "About Acme"
    assert "Acme Payments" in text and "Founded in 2016 in Bengaluru." in text
    assert "alert" not in text and ".x{}" not in text


# ── fetch: the happy path and the pinning ────────────────────────────────
def test_fetch_connects_to_the_approved_ip_only() -> None:
    lookups: list[str] = []

    def rebinding_dns(host: str) -> list[str]:
        lookups.append(host)  # a second answer would be private: it must never be asked
        return ["93.184.216.34"] if len(lookups) == 1 else ["127.0.0.1"]

    transport = Recorder(html())
    page = fetch_page("https://acme.example/about?x=1", 8000, rebinding_dns, transport)
    assert transport.calls == [("93.184.216.34", "acme.example", "/about?x=1")]
    assert lookups == ["acme.example"]  # resolved once, connected to what was checked
    assert page.title == "About Acme" and "Founded in 2016" in page.text


def test_second_approved_ip_is_tried_when_the_first_refuses() -> None:
    transport = Recorder(ConnectionRefusedError(), html())
    fetch_page("https://two-ips.example/", 8000, resolve, transport)
    assert [c[0] for c in transport.calls] == ["93.184.216.40", "93.184.216.41"]


def test_long_pages_are_cut_to_the_token_budget() -> None:
    body = "<p>" + "word " * 5000 + "</p>"
    page = fetch_page("https://acme.example/", 100, resolve, Recorder(html(body=body)))
    assert len(page.text) < 130 and page.text.endswith("[page truncated]")


def test_a_page_over_2mb_is_marked_truncated() -> None:
    raw = RawResponse(200, {"content-type": "text/plain"}, b"short", truncated=True)
    page = fetch_page("https://acme.example/", 8000, resolve, Recorder(raw))
    assert page.text.endswith("[page truncated]")


# ── fetch: redirects ─────────────────────────────────────────────────────
def test_relative_redirect_is_followed_and_rechecked() -> None:
    transport = Recorder(html(301, "", location="/new"), html())
    page = fetch_page("https://acme.example/old", 8000, resolve, transport)
    assert [c[2] for c in transport.calls] == ["/old", "/new"]
    assert str(page.url) == "https://acme.example/new"


def test_redirect_into_the_private_network_is_blocked() -> None:
    transport = Recorder(html(302, "", location="https://evil.example/admin"))
    with pytest.raises(UnsafeURL):
        fetch_page("https://acme.example/", 8000, resolve, transport)
    assert len(transport.calls) == 1  # evil.example was never connected to


def test_redirect_down_to_http_is_blocked() -> None:
    transport = Recorder(html(301, "", location="http://acme.example/"))
    with pytest.raises(UnsafeURL):
        fetch_page("https://acme.example/", 8000, resolve, transport)


def test_too_many_redirects_are_blocked() -> None:
    hops = [html(302, "", location=f"/hop{i}") for i in range(MAX_REDIRECTS + 1)]
    with pytest.raises(UnsafeURL, match="redirects"):
        fetch_page("https://acme.example/", 8000, resolve, Recorder(*hops))


# ── fetch: what the model reads vs what gets retried ─────────────────────
@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (html(404, ""), ToolError),  # the page isn't there: the model's problem
        (html(403, ""), ToolError),
        (html(200, "%PDF", **{"content-type": "application/pdf"}), ToolError),
        (html(200, "x", **{"content-encoding": "gzip"}), ToolError),  # asked for identity
        (html(503, ""), RetryableError),  # the site is struggling: retry + breaker
        (html(429, ""), RetryableError),
        (TimeoutError(), RetryableError),
        (ssl.SSLCertVerificationError("bad cert"), ToolError),  # not transient
    ],
)
def test_failures_land_in_the_right_bucket(
    answer: RawResponse | Exception, error: type[Exception]
) -> None:
    with pytest.raises(error):
        fetch_page("https://acme.example/", 8000, resolve, Recorder(answer))


def test_unreachable_everywhere_is_retryable() -> None:
    transport = Recorder(ConnectionResetError(), ConnectionRefusedError())
    with pytest.raises(RetryableError):
        fetch_page("https://two-ips.example/", 8000, resolve, transport)


def test_unsafe_url_becomes_an_ok_false_result_for_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the real tool function: a blocked URL never crashes the run."""
    monkeypatch.setattr(
        real_tools,
        "fetch_page",
        lambda url, n: (_ for _ in ()).throw(
            UnsafeURL("blocked 'https://10.0.0.1/': non-public address")
        ),
    )
    with pytest.raises(ToolError, match="blocked"):
        tools_mod._real_fetch_page(
            tools_mod.FetchPageInput.model_validate({"url": "https://10.0.0.1/"}), None
        )


# ── search ───────────────────────────────────────────────────────────────
class FakeOpener:
    def __init__(self, answer: bytes | Exception) -> None:
        self.answer = answer
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return io.BytesIO(self.answer)


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(real_tools.TAVILY_URL, code, "x", {}, None)  # type: ignore[arg-type]


def test_search_maps_results_and_sends_the_key_only_in_a_header() -> None:
    answer = {
        "results": [
            {"title": "Acme", "url": "https://acme.example/", "content": "Payments  for SMBs"},
            {"title": "broken", "url": "not a url"},  # skipped, not fatal
            {"content": "no url at all"},  # skipped
        ]
        + [{"title": f"r{i}", "url": f"https://r{i}.example/"} for i in range(10)]
    }
    opener = FakeOpener(json.dumps(answer).encode())
    results = tavily_search("acme payments", "tvly-test-secret", opener)

    assert results[0].title == "Acme" and results[0].snippet == "Payments for SMBs"
    assert len(results) <= real_tools.SEARCH_RESULTS
    req = opener.requests[0]
    assert req.get_header("Authorization") == "Bearer tvly-test-secret"
    assert "tvly-test-secret" not in req.full_url
    assert "tvly-test-secret" not in req.data.decode()  # not in the body either
    assert json.loads(req.data)["query"] == "acme payments"


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (http_error(401), ToolError),  # bad key: say so, don't retry
        (http_error(400), ToolError),
        (http_error(429), RetryableError),
        (http_error(502), RetryableError),
        (urllib.error.URLError("dns"), RetryableError),
        (TimeoutError(), RetryableError),
        (b"<html>not json</html>", RetryableError),
    ],
)
def test_search_failures_land_in_the_right_bucket(
    answer: bytes | Exception, error: type[Exception]
) -> None:
    with pytest.raises(error) as exc:
        tavily_search("q", "tvly-test-secret", FakeOpener(answer))
    assert "tvly-test-secret" not in str(exc.value)  # the key never reaches the model


def test_empty_search_is_an_empty_list() -> None:
    assert tavily_search("q", "k", FakeOpener(b'{"results": []}')) == []


# ── registry ─────────────────────────────────────────────────────────────
def test_real_provider_refuses_to_boot_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_mod.settings, "tools_provider", "real")
    monkeypatch.setattr(tools_mod.settings, "tavily_api_key", None)
    with pytest.raises(RuntimeError, match="TAVILY_API_KEY"):
        tools_mod._build_registry()


def test_real_provider_shows_the_model_the_same_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_mod.settings, "tools_provider", "real")
    monkeypatch.setattr(tools_mod.settings, "tavily_api_key", "tvly-test")
    real = tools_mod._build_registry()
    fake = tools_mod.REGISTRY  # built at import with the default (fake) provider
    assert real.keys() == fake.keys()
    for name in real:
        assert real[name].input_model is fake[name].input_model
        assert real[name].description == fake[name].description
    assert real["fetch_page"].run is tools_mod._real_fetch_page


# ── the pinned transport against a real local TLS server ─────────────────
@pytest.fixture(scope="module")
def tls_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    crypto = pytest.importorskip("cryptography")  # noqa: F841 - only to skip cleanly
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "relay-test.example")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("relay-test.example")]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    d: Path = tmp_path_factory.mktemp("tls")
    (d / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            body = f"<title>t</title><p>host={self.headers['Host']} path={self.path}</p>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(d / "cert.pem", d / "key.pem")
    server.socket = sctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    client_ctx = ssl.create_default_context(cafile=str(d / "cert.pem"))
    yield {"port": server.server_address[1], "ctx": client_ctx}
    server.shutdown()


def test_pinned_transport_verifies_tls_for_the_real_name(tls_server: dict[str, Any]) -> None:
    raw = real_tools.https_get_pinned(
        "127.0.0.1", "relay-test.example", "/about?q=1", 5, tls_server["ctx"], tls_server["port"]
    )
    assert raw.status == 200
    assert b"host=relay-test.example" in raw.body  # Host header = the URL's name, not the IP
    assert b"path=/about?q=1" in raw.body


def test_pinned_transport_rejects_a_certificate_for_another_name(
    tls_server: dict[str, Any],
) -> None:
    # same IP, but the URL claimed a different site: the certificate doesn't match
    with pytest.raises(ssl.SSLCertVerificationError):
        real_tools.https_get_pinned(
            "127.0.0.1", "other.example", "/", 5, tls_server["ctx"], tls_server["port"]
        )
