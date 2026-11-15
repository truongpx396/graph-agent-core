"""`HttpxPageFetcher` (`app/adapters/outbound/web/page_fetch.py`): one plain GET for the ingestion pipeline, following no redirect, connecting to the address it
checked and bounding what it reads (PRODUCTION_REVIEW.md R20)."""
import datetime
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.adapters.outbound.web import page_fetch
from app.core.url_safety import UnsafeURLError
from app.ports.outbound.web import PageFetchFailedError

REAL_CLIENT = httpx.AsyncClient
PUBLIC = "93.184.216.34"  # a global address; the tests never connect to it, a MockTransport answers


@pytest.fixture(autouse=True)
def resolver(monkeypatch):
    """The fetcher resolves the host itself now (it connects to what it checked): a test must not reach a real resolver."""
    answers = {"addresses": [PUBLIC], "asked": []}

    async def resolve_checked_async(url):
        answers["asked"].append(url)
        return list(answers["addresses"])

    monkeypatch.setattr(page_fetch, "resolve_checked_async", resolve_checked_async)
    return answers


def _serve(monkeypatch, handler, seen=None):
    def make(**kwargs):
        if seen is not None:
            seen.update(kwargs)
        return REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(page_fetch.httpx, "AsyncClient", make)


async def test_a_page_comes_back_with_its_status_type_text_and_size(monkeypatch):
    seen = {}
    _serve(monkeypatch, lambda request: httpx.Response(200, text="hello", headers={"content-type": "text/html"}), seen)

    page = await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=1000)

    assert (page.status_code, page.content_type, page.text, page.size) == (200, "text/html", "hello", 5)
    assert seen["follow_redirects"] is False


async def test_a_redirect_is_returned_not_followed(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(302, headers={"location": "http://169.254.169.254/"}))

    page = await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=1000)

    assert page.status_code == 302


async def test_a_body_over_the_limit_is_not_decoded_but_its_size_is_reported(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 50))

    page = await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=10)

    assert (page.text, page.size) == ("", 50)


async def test_a_request_that_does_not_complete_raises_the_ports_error(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused")

    _serve(monkeypatch, refuse)

    with pytest.raises(PageFetchFailedError, match="refused"):
        await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=10)


async def test_it_says_who_is_asking(monkeypatch):
    agents = []
    _serve(monkeypatch, lambda request: (agents.append(request.headers["user-agent"]), httpx.Response(200, text="ok"))[1])

    await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=10)

    assert agents == ["agent-core-demo-ingestor/1.0"]


# --- It connects to the address it checked (DNS rebinding) ----------------------------------------------------------------


async def test_the_request_goes_to_the_checked_address_with_the_real_hostname_as_host_and_tls_server_name(monkeypatch):
    """A name that answers differently the second time cannot reach an internal address: the fetcher never resolves the name again."""
    seen = []
    _serve(monkeypatch, lambda request: (seen.append(request), httpx.Response(200, text="ok"))[1])

    await page_fetch.HttpxPageFetcher().fetch("https://example.com/a?b=1", max_bytes=100)

    (request,) = seen
    assert request.url.host == PUBLIC and request.url.path == "/a" and request.url.query == b"b=1"
    assert request.headers["host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"


async def test_a_port_in_the_url_is_kept_on_the_connection_and_in_the_host_header(monkeypatch):
    seen = []
    _serve(monkeypatch, lambda request: (seen.append(request), httpx.Response(200, text="ok"))[1])

    await page_fetch.HttpxPageFetcher().fetch("https://example.com:8443/a", max_bytes=100)

    assert seen[0].url.port == 8443 and seen[0].headers["host"] == "example.com:8443"


async def test_an_ipv6_address_is_connected_to_as_an_address(monkeypatch, resolver):
    resolver["addresses"] = ["2606:2800:220:1:248:1893:25c8:1946"]
    seen = []
    _serve(monkeypatch, lambda request: (seen.append(request), httpx.Response(200, text="ok"))[1])

    await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=100)

    assert seen[0].url.host == "2606:2800:220:1:248:1893:25c8:1946" and seen[0].headers["host"] == "example.com"


async def test_the_name_is_resolved_once_per_fetch_by_the_guard_and_never_by_the_connection(monkeypatch, resolver):
    _serve(monkeypatch, lambda request: httpx.Response(200, text="ok"))

    await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=100)

    assert resolver["asked"] == ["https://example.com/a"]


async def test_a_host_the_guard_refuses_is_never_requested_and_is_reported_as_a_failed_fetch(monkeypatch):
    called = []

    async def refuse(url):
        raise UnsafeURLError("URL resolves to a disallowed address (10.0.0.5) — refused")

    monkeypatch.setattr(page_fetch, "resolve_checked_async", refuse)
    _serve(monkeypatch, lambda request: (called.append(request), httpx.Response(200, text="no"))[1])

    with pytest.raises(PageFetchFailedError, match="refused"):
        await page_fetch.HttpxPageFetcher().fetch("https://rebinder.example/a", max_bytes=100)

    assert called == []


async def test_an_address_that_refuses_the_connection_is_followed_by_the_next_one(monkeypatch, resolver):
    resolver["addresses"] = ["93.184.216.34", "93.184.216.35"]
    tried = []

    def handler(request):
        tried.append(request.url.host)
        if request.url.host == "93.184.216.34":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, text="from the second")

    _serve(monkeypatch, handler)

    page = await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=100)

    assert tried == ["93.184.216.34", "93.184.216.35"] and page.text == "from the second"


async def test_when_every_address_refuses_the_connection_it_is_a_failed_fetch(monkeypatch, resolver):
    resolver["addresses"] = ["93.184.216.34", "93.184.216.35"]

    def refuse(request):
        raise httpx.ConnectError("refused")

    _serve(monkeypatch, refuse)

    with pytest.raises(PageFetchFailedError, match="refused"):
        await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=100)


async def test_a_failure_after_the_connection_is_made_is_not_retried_on_another_address(monkeypatch, resolver):
    """A timeout or a protocol error mid-request is not "this address is down": the request may have been received."""
    resolver["addresses"] = ["93.184.216.34", "93.184.216.35"]
    tried = []

    def handler(request):
        tried.append(request.url.host)
        raise httpx.ReadTimeout("slow")

    _serve(monkeypatch, handler)

    with pytest.raises(PageFetchFailedError):
        await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=100)

    assert tried == ["93.184.216.34"]


# --- It streams the body and stops at the limit --------------------------------------------------------------------------


class _CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks, chunk_size=10):
        self.served = 0
        self._chunks, self._size = chunks, chunk_size

    async def __aiter__(self):
        for _ in range(self._chunks):
            self.served += self._size
            yield b"x" * self._size


async def test_a_response_larger_than_the_limit_is_abandoned_without_being_read_whole(monkeypatch):
    """It used to be read in full and compared to the limit afterwards: a large response sat in memory before it was refused."""
    stream = _CountingStream(chunks=100_000)  # a gigabyte of ten-byte chunks, if anyone reads it
    _serve(monkeypatch, lambda request: httpx.Response(200, stream=stream))

    page = await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=50)

    assert stream.served <= 70, f"{stream.served} bytes were read for a 50-byte limit"
    assert page.text == "" and page.size > 50


async def test_a_declared_length_over_the_limit_is_refused_before_a_byte_is_read(monkeypatch):
    stream = _CountingStream(chunks=10)
    _serve(monkeypatch, lambda request: httpx.Response(200, headers={"content-length": "5000"}, stream=stream))

    page = await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=100)

    assert stream.served == 0 and page.text == "" and page.size == 5000


async def test_a_body_exactly_at_the_limit_is_returned_whole(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(200, content=b"y" * 100))

    page = await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=100)

    assert page.text == "y" * 100 and page.size == 100


async def test_the_declared_character_set_is_used_to_decode(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(200, content="café".encode("latin-1"), headers={"content-type": "text/html; charset=latin-1"}))

    page = await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=100)

    assert page.text == "café"


async def test_a_body_that_is_not_valid_in_its_character_set_is_decoded_with_replacements_not_an_error(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(200, content=b"ok \xff\xfe", headers={"content-type": "text/plain; charset=utf-8"}))

    page = await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=100)

    assert page.text.startswith("ok ")


# --- A real TLS connection to an address, verified against the real name ------------------------------------------------


def _certificate_for(name):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()),
    )


class _TlsServer:
    """A local HTTPS server on 127.0.0.1 whose certificate is for `name`; records the Host header and the server name it was asked for."""

    def __init__(self, name, tmp_path):
        cert, key = _certificate_for(name)
        self.cert_path, self.key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
        self.cert_path.write_bytes(cert)
        self.key_path.write_bytes(key)
        self.hosts, self.server_names = [], []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hosts.append(self.headers["Host"])
                body = b"served over TLS"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert_path, self.key_path)
        context.sni_callback = lambda sock, server_name, ctx: outer.server_names.append(server_name)
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _trusting(monkeypatch, server):
    context = ssl.create_default_context(cafile=str(server.cert_path))
    monkeypatch.setattr(page_fetch.httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(verify=context, **kwargs))


async def test_a_real_tls_request_goes_to_the_address_with_the_real_hostname_as_server_name(monkeypatch, tmp_path, resolver):
    """The connection is to 127.0.0.1 (standing in for the checked address); the certificate is for `pinned.test`, which is what the fetcher asked to verify."""
    server = _TlsServer("pinned.test", tmp_path)
    try:
        resolver["addresses"] = ["127.0.0.1"]
        _trusting(monkeypatch, server)

        page = await page_fetch.HttpxPageFetcher().fetch(f"https://pinned.test:{server.port}/x", max_bytes=1000)

        assert page.text == "served over TLS"
        assert server.server_names == ["pinned.test"], "the TLS server name is the real hostname, not the IP address"
        assert server.hosts == [f"pinned.test:{server.port}"]
    finally:
        server.close()


async def test_a_certificate_for_another_name_is_refused_because_it_is_verified_against_the_real_hostname(monkeypatch, tmp_path, resolver):
    """Connecting by address must not turn certificate checking into 'is it valid for this IP' or 'is it valid for anything'."""
    server = _TlsServer("someone-else.test", tmp_path)
    try:
        resolver["addresses"] = ["127.0.0.1"]
        _trusting(monkeypatch, server)

        with pytest.raises(PageFetchFailedError):
            await page_fetch.HttpxPageFetcher().fetch(f"https://pinned.test:{server.port}/x", max_bytes=1000)

        assert server.hosts == [], "no request was sent over a connection whose certificate did not match"
    finally:
        server.close()
