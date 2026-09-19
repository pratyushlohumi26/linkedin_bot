from __future__ import annotations

import gzip
import ipaddress
import logging
import shutil
import socket
import ssl
import subprocess
import threading
import time
import tracemalloc
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from telegram_bot import http_fetcher
from telegram_bot.http_fetcher import (
    FetchError,
    SafeFetcher,
    resolve_public_addresses,
    validate_public_url,
)


def config(**overrides):
    values = dict(
        timeout_seconds=1,
        connect_timeout_seconds=0.5,
        total_timeout_seconds=3,
        max_attempts=2,
        max_response_bytes=1024,
        max_redirects=5,
        min_interval_seconds=0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def network(monkeypatch):
    """Keep validation/transport real; substitute DNS and numeric socket destinations only."""
    state = SimpleNamespace(
        records=[], connects=[], dns=[], addresses=["8.8.8.8"], server=None, dns_hook=None
    )
    servers = []
    threads = []
    original_connect = socket.socket.connect

    def lookup(host, port, family=0, type=0, proto=0, flags=0):
        state.dns.append((host, port))
        addresses = state.dns_hook(host, port) if state.dns_hook else state.addresses
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port, 0, 0) if ":" in address else (address, port),
            )
            for address in addresses
        ]

    def connect(sock, address):
        ipaddress.ip_address(address[0])
        state.connects.append(address)
        assert state.server is not None, "Unexpected connection before local server started"
        return original_connect(sock, state.server.server_address)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(socket.socket, "connect", connect)

    def serve(respond=None, tls=None, ipv6=False):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                state.records.append((self.path, dict(self.headers), time.monotonic()))
                try:
                    if respond is not None:
                        respond(self)
                    else:
                        reply(self, body=b"<html>article</html>")
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    pass

            def log_message(self, *args):
                pass

        class Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6 if ipv6 else socket.AF_INET
            daemon_threads = True

        server = Server(("::1" if ipv6 else "127.0.0.1", 0), Handler)
        if tls:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        state.server = server
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        servers.append(server)
        threads.append(thread)
        return state

    state.serve = serve
    yield state
    for server in servers:
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join(timeout=2)


def reply(handler, status=200, body=b"ok", headers=None):
    handler.send_response(status)
    headers = headers or {}
    for key, value in headers.items():
        handler.send_header(key, value)
    if not any(key.lower() == "content-length" for key in headers):
        handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        ("HTTPS://Example.COM:443/a?token=secret#fragment", "https://example.com/a?token=secret"),
        ("http://example.com", "http://example.com/"),
        ("https://example.com./", "https://example.com/"),
        ("https://bücher.de/café", "https://xn--bcher-kva.de/caf%C3%A9"),
        ("http://8.8.8.8:80/a", "http://8.8.8.8/a"),
        ("https://[2001:4860:4860:0:0:0:0:8888]/", "https://[2001:4860:4860::8888]/"),
        ("https://[::ffff:8.8.8.8]/", f"https://[{ipaddress.ip_address('::ffff:8.8.8.8')}]/"),
    ],
)
def test_normalization(url, normalized):
    assert validate_public_url(url) == normalized


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a",
        "//example.com/a",
        "example.com",
        "https://user:secret@example.com/",
        "https://user@example.com/",
        "https://@example.com/",
        "https://example.com:8080/",
        "https://example.com:0/",
        "https://example.com:bad/",
        "http://127.0.0.1/",
        "http://0.0.0.0/",
        "http://10.0.0.1/",
        "http://172.16.1.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://168.63.129.16/?comp=versions",
        "http://[::ffff:168.63.129.16]/",
        "http://100.100.100.200/",
        "http://192.0.2.1/",
        "http://224.0.0.1/",
        "http://255.255.255.255/",
        "http://[::1]/",
        "http://[::]/",
        "http://[fc00::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://[fec0::1]/",
        "http://[ff02::1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[fe80::1%25eth0]/",
        "http://localhost/",
        "http://foo.localhost/",
        "http://metadata.google.internal/",
        "http://intranet/",
        "http://thing.local/",
        "http://thing.internal/",
        "http://thing.home.arpa/",
        "http://127.1/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://0x7f.0.0.1/",
        "http://0x7f.0x0.0x0.0x1/",
        "http://example.com\\@127.0.0.1/",
        "https://example.com\n/",
        "https://example.com/\r",
        "https://example.com/\x00",
        "https://example.com/\x7f",
        " https://example.com/",
        "https://exa mple.com/",
        "https://%31%32%37.0.0.1/",
        "https://[broken]/",
        "https://example.com/%0d%0aInjected:value",
        "https://example.com/?x=%00",
    ],
)
def test_reject_unsafe_urls(url):
    with pytest.raises(FetchError) as caught:
        validate_public_url(url)
    assert caught.value.status in {"invalid_url", "blocked", "unsupported"}
    assert caught.value.retryable is False
    assert url not in str(caught.value)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("private", ["127.0.0.1", "169.254.169.254", "::1", "::ffff:10.0.0.1"])
def test_resolver_rejects_entire_mixed_answer(network, private):
    network.addresses = ["8.8.8.8", private]
    with pytest.raises(FetchError, match="public") as caught:
        resolve_public_addresses("articles.example.org", 443)
    assert caught.value.status == "blocked"
    assert network.connects == []


def test_resolver_normalizes_deduplicates_and_supports_ipv6(network):
    network.addresses = ["8.8.8.8", "8.8.8.8", "2001:4860:4860:0:0:0:0:8888"]
    assert resolve_public_addresses("articles.example.org", 443) == [
        "8.8.8.8",
        "2001:4860:4860::8888",
    ]
    assert resolve_public_addresses("::ffff:8.8.8.8", 443) == [
        str(ipaddress.ip_address("::ffff:8.8.8.8"))
    ]
    assert len(network.dns) == 1


@pytest.mark.parametrize("port", [0, 22, 8080])
def test_resolver_rejects_other_ports(network, port):
    with pytest.raises(FetchError):
        resolve_public_addresses("articles.example.org", port)
    assert network.dns == []


@pytest.mark.parametrize(
    "host", ["articles.\u200bexample.org", "articles.example.org\n", "x\x00.example.org"]
)
def test_resolver_rejects_control_characters_before_idna(network, host):
    with pytest.raises(FetchError) as caught:
        resolve_public_addresses(host, 443)
    assert caught.value.status == "invalid_url"
    assert network.dns == []


def test_pins_validated_ip_no_second_dns_lookup(network, caplog):
    network.serve()
    network.dns_hook = lambda *_: ["8.8.8.8"] if len(network.dns) == 1 else ["127.0.0.1"]
    with caplog.at_level(logging.DEBUG):
        response = SafeFetcher(config()).fetch("http://articles.example.org/story?token=secret")
    assert response.status_code == 200
    assert response.body == b"<html>article</html>"
    assert response.requested_url == "http://articles.example.org/story?token=secret"
    assert response.url == response.requested_url
    assert response.attempts == 1
    assert response.elapsed_seconds > 0
    assert response.headers["content-length"] == str(len(response.body))
    assert all(key == key.lower() for key in response.headers)
    assert network.dns == [("articles.example.org", 80)]
    assert network.connects == [("8.8.8.8", 80)]
    assert network.records[0][1]["Host"] == "articles.example.org"
    assert "secret" not in caplog.text


@pytest.mark.parametrize("http10", [False, True])
def test_connection_close_response_returns_complete_body(network, http10):
    def respond(h):
        if http10:
            h.wfile.write(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok")
            h.wfile.flush()
            h.close_connection = True
        else:
            reply(h, body=b"ok", headers={"Connection": "close"})

    network.serve(respond)
    result = SafeFetcher(config()).fetch("http://articles.example.org/")
    assert result.body == b"ok"
    assert result.attempts == 1


def test_public_ipv6_connection(network):
    if not socket.has_ipv6:
        pytest.skip("IPv6 sockets unavailable")
    network.addresses = ["2001:4860:4860::8888"]
    try:
        network.serve(ipv6=True)
    except OSError:
        pytest.skip("IPv6 loopback unavailable")
    response = SafeFetcher(config()).fetch("http://[2001:4860:4860::8888]/")
    assert response.status_code == 200
    assert network.dns == []
    assert network.connects == [("2001:4860:4860::8888", 80, 0, 0)]
    assert network.records[0][1]["Host"] == "[2001:4860:4860::8888]"


@pytest.fixture
def tls_contexts(tmp_path, monkeypatch):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is needed only to generate an ephemeral test TLS certificate")
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-subj",
            "/CN=articles.example.org",
            "-addext",
            "subjectAltName=DNS:articles.example.org",
        ],
        check=True,
        capture_output=True,
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    names = []
    server.set_servername_callback(lambda sock, name, context: names.append(name))
    client = ssl.create_default_context(cafile=str(cert))
    assert client.check_hostname and client.verify_mode == ssl.CERT_REQUIRED
    # Substitute only the trust store, not TLS or hostname verification.
    monkeypatch.setattr(ssl, "create_default_context", lambda: client)
    return server, names


def test_tls_preserves_sni_and_verifies_original_hostname(network, tls_contexts):
    server, names = tls_contexts
    network.serve(tls=server)
    response = SafeFetcher(config()).fetch("https://articles.example.org/article")
    assert response.status_code == 200
    assert network.connects == [("8.8.8.8", 443)]
    assert network.dns == [("articles.example.org", 443)]
    assert names == ["articles.example.org"]
    assert network.records[0][1]["Host"] == "articles.example.org"


def test_tls_mismatched_hostname_not_retried(network, tls_contexts):
    server, names = tls_contexts
    network.serve(tls=server)
    with pytest.raises(FetchError) as caught:
        SafeFetcher(config()).fetch("https://wrong.example.org/?secret=hidden")
    assert caught.value.status == "fetch_failed"
    assert caught.value.retryable is False
    assert len(network.connects) == 1
    assert names == ["wrong.example.org"]
    assert "hidden" not in str(caught.value)
    assert network.records == []


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/",
        "http://[::ffff:127.0.0.1]/",
        "//localhost/",
        "http://user:secret@articles.example.org/",
        "file:///etc/passwd",
    ],
)
def test_redirect_to_unsafe_target_blocked(network, location):
    network.serve(lambda h: reply(h, 302, headers={"Location": location}))
    with pytest.raises(FetchError) as caught:
        SafeFetcher(config()).fetch("http://articles.example.org/")
    assert caught.value.retryable is False
    assert len(network.records) == len(network.connects) == 1


def test_redirect_dns_revalidated(network):
    network.serve(lambda h: reply(h, 302, headers={"Location": "/next"}))
    network.dns_hook = lambda *_: ["8.8.8.8"] if len(network.dns) == 1 else ["127.0.0.1"]
    with pytest.raises(FetchError) as caught:
        SafeFetcher(config()).fetch("http://articles.example.org/")
    assert caught.value.status == "blocked"
    assert len(network.dns) == 2
    assert len(network.connects) == 1


def test_relative_redirect_final_url_and_attempt_count(network):
    def respond(h):
        if h.path == "/old":
            reply(h, 302, headers={"Location": "/new?x=1"})
        else:
            reply(h, body=b"final")

    network.serve(respond)
    result = SafeFetcher(config()).fetch("http://articles.example.org/old")
    assert result.body == b"final"
    assert result.url == "http://articles.example.org/new?x=1"
    assert result.requested_url == "http://articles.example.org/old"
    assert result.attempts == 1
    assert len(network.records) == 2


def test_redirect_limit_not_reset_by_retry(network):
    network.serve(lambda h: reply(h, 302, headers={"Location": "/again"}))
    with pytest.raises(FetchError) as caught:
        SafeFetcher(config(max_redirects=2)).fetch("http://articles.example.org/start")
    assert caught.value.status == "fetch_failed"
    assert caught.value.retryable is False
    assert len(network.records) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 418, 501, 505])
def test_http_error_status_returned_without_retry(network, status):
    network.serve(lambda h: reply(h, status, b"provider error secret"))
    response = SafeFetcher(config()).fetch("http://articles.example.org/")
    assert response.status_code == status
    assert response.attempts == 1
    assert response.body == b"provider error secret"
    assert len(network.records) == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_selective_retries_then_success(network, status):
    def respond(h):
        reply(h, status if len(network.records) == 1 else 200, headers={"Retry-After": "0"})

    network.serve(respond)
    response = SafeFetcher(config()).fetch("http://articles.example.org/")
    assert response.status_code == 200
    assert response.attempts == 2
    assert len(network.records) == 2


def test_exhausted_status_retries_return_last_response(network):
    network.serve(lambda h: reply(h, 503, headers={"Retry-After": "0"}))
    response = SafeFetcher(config()).fetch("http://articles.example.org/")
    assert response.status_code == 503
    assert response.attempts == 2


@pytest.mark.parametrize(
    "retry_after", ["99999", format_datetime(datetime.now(UTC) + timedelta(days=1))]
)
def test_retry_after_exceeding_deadline_returns_http_status(network, retry_after):
    network.serve(lambda h: reply(h, 429, headers={"Retry-After": retry_after}))
    start = time.monotonic()
    response = SafeFetcher(config(total_timeout_seconds=0.2)).fetch("http://articles.example.org/")
    assert response.status_code == 429
    assert response.attempts == 1
    assert time.monotonic() - start < 0.5


def test_retry_after_waits_without_busy_loop(network):
    network.serve(
        lambda h: reply(h, 429 if len(network.records) == 1 else 200, headers={"Retry-After": "1"})
    )
    response = SafeFetcher(config()).fetch("http://articles.example.org/")
    assert response.status_code == 200
    assert network.records[1][2] - network.records[0][2] >= 0.98


def test_connect_failure_retried_at_socket_boundary(network, monkeypatch):
    network.serve()
    connect = socket.socket.connect
    calls = []

    def transient(sock, address):
        calls.append(address)
        if len(calls) == 1:
            raise ConnectionRefusedError("provider address secret")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", transient)
    response = SafeFetcher(config()).fetch("http://articles.example.org/")
    assert response.attempts == 2
    assert len(calls) == 2


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate", "raw-deflate", "gzip-members"])
def test_decoded_body_limit(network, encoding):
    body = b"x" * 100_000
    headers = {}
    if encoding == "gzip":
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    elif encoding == "gzip-members":
        body = gzip.compress(b"x" * 512) + gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    elif encoding == "deflate":
        body = zlib.compress(body)
        headers["Content-Encoding"] = "deflate"
    elif encoding == "raw-deflate":
        body = zlib.compress(body, wbits=-zlib.MAX_WBITS)
        headers["Content-Encoding"] = "deflate"
    network.serve(lambda h: reply(h, body=body, headers=headers))
    with pytest.raises(FetchError) as caught:
        SafeFetcher(config()).fetch("http://articles.example.org/")
    assert caught.value.status == "too_large"
    assert caught.value.retryable is False
    assert len(network.records) == 1


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate", "raw-deflate", "gzip-members"])
def test_supported_decoding_exact_limit(network, encoding):
    plain = b"article " * 128
    body = plain
    headers = {}
    if encoding == "gzip":
        body = gzip.compress(plain)
        headers["Content-Encoding"] = "gzip"
    elif encoding == "gzip-members":
        body = gzip.compress(plain[:512]) + gzip.compress(plain[512:])
        headers["Content-Encoding"] = "gzip"
    elif encoding == "deflate":
        body = zlib.compress(plain)
        headers["Content-Encoding"] = "deflate"
    elif encoding == "raw-deflate":
        body = zlib.compress(plain, wbits=-zlib.MAX_WBITS)
        headers["Content-Encoding"] = "deflate"
    network.serve(lambda h: reply(h, body=body, headers=headers))
    assert SafeFetcher(config()).fetch("http://articles.example.org/").body == plain


@pytest.mark.parametrize("encoding", ["br", "zstd", "gzip, gzip"])
def test_unsupported_content_encoding(network, encoding):
    network.serve(lambda h: reply(h, headers={"Content-Encoding": encoding}))
    with pytest.raises(FetchError) as caught:
        SafeFetcher(config()).fetch("http://articles.example.org/")
    assert caught.value.status == "unsupported"


def test_truncated_compression_not_retried(network):
    network.serve(
        lambda h: reply(
            h, body=gzip.compress(b"article")[:-5], headers={"Content-Encoding": "gzip"}
        )
    )
    with pytest.raises(FetchError) as caught:
        SafeFetcher(config()).fetch("http://articles.example.org/")
    assert caught.value.status == "fetch_failed"
    assert caught.value.retryable is False
    assert len(network.records) == 1


def test_chunked_without_length_is_bounded(network):
    def respond(h):
        h.send_response(200)
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        h.wfile.write(b"800\r\n" + b"x" * 2048 + b"\r\n0\r\n\r\n")

    network.serve(respond)
    with pytest.raises(FetchError) as caught:
        SafeFetcher(config()).fetch("http://articles.example.org/")
    assert caught.value.status == "too_large"


def test_expired_caller_deadline_makes_no_dns_or_connections(network):
    with pytest.raises(FetchError) as caught:
        SafeFetcher(config()).fetch("http://articles.example.org/", deadline=time.monotonic() - 1)
    assert caught.value.status == "timeout"
    assert network.dns == network.connects == []


def test_slow_resolver_cannot_outlive_deadline(network):
    finished = threading.Event()
    release = threading.Event()

    def slow(*args):
        release.wait(1)
        finished.set()
        return ["8.8.8.8"]

    network.dns_hook = slow
    start = time.monotonic()
    try:
        with pytest.raises(FetchError) as caught:
            SafeFetcher(config()).fetch("http://articles.example.org/", deadline=start + 0.08)
        assert caught.value.status == "timeout"
        assert time.monotonic() - start < 0.4
        assert network.connects == []
    finally:
        release.set()
        assert finished.wait(1)


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_slow_trickle_cannot_extend_total_deadline(network, phase):
    def respond(h):
        if phase == "headers":
            for chunk in b"HTTP/1.1 200 OK\r\nX-Slow: xxxxxxxxxxxxxxxxxxxxx\r\n\r\n":
                h.wfile.write(bytes([chunk]))
                h.wfile.flush()
                time.sleep(0.02)
        else:
            h.send_response(200)
            h.send_header("Content-Length", "100")
            h.end_headers()
            for _ in range(100):
                h.wfile.write(b"x")
                h.wfile.flush()
                time.sleep(0.02)

    network.serve(respond)
    start = time.monotonic()
    with pytest.raises(FetchError) as caught:
        SafeFetcher(config(timeout_seconds=0.1, total_timeout_seconds=0.15)).fetch(
            "http://articles.example.org/"
        )
    assert caught.value.status == "timeout"
    assert time.monotonic() - start < 0.5


def test_read_timeout_retried(network):
    def respond(h):
        if len(network.records) == 1:
            time.sleep(0.15)
        reply(h)

    network.serve(respond)
    result = SafeFetcher(config(timeout_seconds=0.05)).fetch("http://articles.example.org/")
    assert result.attempts == 2
    assert result.status_code == 200


def test_environment_proxy_netrc_and_cookies_not_used(network, monkeypatch, tmp_path):
    netrc = tmp_path / ".netrc"
    netrc.write_text("machine articles.example.org login user password secret\n")
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        monkeypatch.setenv(key, "http://user:secret@127.0.0.1:1")

    def respond(h):
        if h.path == "/":
            reply(h, 302, headers={"Location": "/final", "Set-Cookie": "auth=secret"})
        else:
            reply(h)

    network.serve(respond)
    fetcher = SafeFetcher(config())
    assert fetcher.fetch("http://articles.example.org/").status_code == 200
    assert fetcher.fetch("http://articles.example.org/final").status_code == 200
    assert all(address[0] == "8.8.8.8" for address in network.connects)
    for _, headers, _ in network.records:
        assert "Cookie" not in headers
        assert "Authorization" not in headers
        assert "Proxy-Authorization" not in headers


def test_domain_throttling_shared_and_thread_safe(network):
    network.serve()
    interval = 0.08
    fetchers = [SafeFetcher(config(min_interval_seconds=interval)) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda f: f.fetch("http://throttle.example.org/"), fetchers))
    assert all(result.status_code == 200 for result in results)
    times = sorted(record[2] for record in network.records)
    assert all(b - a >= interval * 0.8 for a, b in zip(times, times[1:], strict=False))


def test_throttle_wait_respects_caller_deadline(network):
    network.serve()
    fetcher = SafeFetcher(config(min_interval_seconds=0.5))
    fetcher.fetch("http://throttle-deadline.example.org/")
    start = time.monotonic()
    with pytest.raises(FetchError) as caught:
        fetcher.fetch("http://throttle-deadline.example.org/", deadline=start + 0.05)
    assert caught.value.status == "timeout"
    assert time.monotonic() - start < 0.3
    assert len(network.records) == 1


def test_dns_error_message_and_logs_never_contain_provider_details(network, caplog):
    def fail(*args):
        raise socket.gaierror(socket.EAI_NONAME, "provider detail with secret")

    network.dns_hook = fail
    with caplog.at_level(logging.DEBUG), pytest.raises(FetchError) as caught:
        SafeFetcher(config()).fetch("http://articles.example.org/?token=secret")
    error = caught.value
    assert error.status == "fetch_failed"
    assert error.retryable is False
    assert error.message == str(error)
    assert "secret" not in str(error)
    assert "provider detail" not in caplog.text
    assert "secret" not in caplog.text
    assert error.__suppress_context__


@pytest.mark.parametrize("status", [204, 304])
def test_bodyless_status_does_not_decode_representation_metadata(network, status):
    network.serve(
        lambda h: reply(
            h, status, b"", headers={"Content-Encoding": "br", "Content-Length": "999999"}
        )
    )
    response = SafeFetcher(config()).fetch("http://articles.example.org/")
    assert response.status_code == status
    assert response.body == b""


def test_response_repr_does_not_disclose_sensitive_fields(network):
    network.serve(
        lambda h: reply(h, body=b"provider-secret", headers={"Set-Cookie": "cookie-secret"})
    )
    response = SafeFetcher(config()).fetch("http://articles.example.org/?token=query-secret")
    assert "secret" not in repr(response)
    assert "example.org" not in repr(response)


def test_redirect_across_hosts_updates_host_and_keeps_no_cookies(network):
    def respond(h):
        if h.path == "/":
            reply(
                h,
                307,
                headers={"Location": "http://next.example.org/final", "Set-Cookie": "secret=token"},
            )
        else:
            reply(h)

    network.serve(respond)
    result = SafeFetcher(config()).fetch("http://articles.example.org/")
    assert result.url == "http://next.example.org/final"
    assert network.dns == [("articles.example.org", 80), ("next.example.org", 80)]
    assert network.records[1][1]["Host"] == "next.example.org"
    assert "Cookie" not in network.records[1][1]


def test_status_retry_cannot_rebind_to_private_address(network):
    network.serve(lambda h: reply(h, 503, headers={"Retry-After": "0"}))
    network.dns_hook = lambda *_: ["8.8.8.8"] if len(network.dns) == 1 else ["127.0.0.1"]
    with pytest.raises(FetchError) as caught:
        SafeFetcher(config()).fetch("http://articles.example.org/")
    assert caught.value.status == "blocked"
    assert len(network.records) == 1


def test_temporary_dns_error_retried(network):
    network.serve()

    def resolve(*_):
        if len(network.dns) == 1:
            raise socket.gaierror(socket.EAI_AGAIN, "temporary provider detail")
        return ["8.8.8.8"]

    network.dns_hook = resolve
    result = SafeFetcher(config()).fetch("http://articles.example.org/")
    assert result.attempts == 2
    assert len(network.records) == 1


def test_dns_workers_remain_bounded_after_caller_timeouts(network):
    release = threading.Event()
    entered = []
    completed = []

    def stall(*args):
        entered.append(threading.get_ident())
        release.wait(2)
        completed.append(threading.get_ident())
        return ["8.8.8.8"]

    network.dns_hook = stall

    def fetch(_):
        with pytest.raises(FetchError) as caught:
            SafeFetcher(config(total_timeout_seconds=0.1, max_attempts=1)).fetch(
                "http://articles.example.org/"
            )
        return caught.value.status

    try:
        with ThreadPoolExecutor(max_workers=16) as executor:
            assert list(executor.map(fetch, range(16))) == ["timeout"] * 16
        assert 0 < len(entered) <= 8
        assert network.connects == []
    finally:
        release.set()
        end = time.monotonic() + 1
        while len(completed) < len(entered) and time.monotonic() < end:
            time.sleep(0.01)
        assert len(completed) == len(entered)


@pytest.mark.parametrize(
    "headers,body",
    [
        ({"Content-Length": "-1"}, b""),
        ({"Content-Length": "invalid"}, b""),
        ({"Content-Length": "1, 1"}, b"x"),
        ({"Content-Length": "10", "Connection": "close"}, b"x"),
        ({"Content-Length": "1", "Transfer-Encoding": "chunked"}, b"0\r\n\r\n"),
    ],
)
def test_invalid_http_framing_is_safe_and_not_retried(network, headers, body):
    def respond(h):
        reply(h, body=body, headers=headers)
        h.close_connection = True

    network.serve(respond)
    with pytest.raises(FetchError) as caught:
        SafeFetcher(config()).fetch("http://articles.example.org/?token=secret")
    assert caught.value.status == "fetch_failed"
    assert caught.value.retryable is False
    assert len(network.records) == 1
    assert "secret" not in str(caught.value)


def test_malformed_response_headers_cannot_log_query_or_provider_data(network, caplog):
    def respond(h):
        h.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nMalformed secret\r\n\r\nok")
        h.wfile.flush()
        h.close_connection = True

    network.serve(respond)
    with caplog.at_level(logging.DEBUG):
        result = SafeFetcher(config()).fetch("http://articles.example.org/?token=secret")
    assert result.status_code == 200
    assert "secret" not in caplog.text


def test_redirect_and_retry_share_one_deadline(network):
    def respond(h):
        if h.path == "/":
            reply(h, 302, headers={"Location": "/slow"})
        else:
            time.sleep(0.06)
            reply(h, 503, headers={"Retry-After": "0"})

    network.serve(respond)
    start = time.monotonic()
    with pytest.raises(FetchError) as caught:
        SafeFetcher(config(total_timeout_seconds=0.1)).fetch("http://articles.example.org/")
    assert caught.value.status == "timeout"
    assert time.monotonic() - start < 0.4
    assert len(network.records) == 3


def test_throttle_wait_does_not_delay_a_different_host(network):
    network.serve()
    fetcher = SafeFetcher(config(min_interval_seconds=0.5))
    fetcher.fetch("http://host-one.example.org/")
    response = fetcher.fetch("http://host-two.example.org/", deadline=time.monotonic() + 0.2)
    assert response.status_code == 200


def test_compression_bomb_is_rejected_without_expanding_in_memory(network):
    bomb = gzip.compress(b"x" * 20_000_000)
    network.serve(lambda h: reply(h, body=bomb, headers={"Content-Encoding": "gzip"}))
    tracemalloc.start()
    try:
        with pytest.raises(FetchError) as caught:
            SafeFetcher(config()).fetch("http://articles.example.org/")
        assert caught.value.status == "too_large"
        assert tracemalloc.get_traced_memory()[1] < 2_000_000
    finally:
        tracemalloc.stop()


def test_domain_throttle_bookkeeping_is_bounded():
    names = [f"bounded-{number}.example.org" for number in range(http_fetcher._MAX_DOMAINS + 1)]
    deadline = time.monotonic() + 5
    try:
        with pytest.raises(FetchError) as caught:
            for host in names:
                http_fetcher._throttle(host, 10, deadline)
        assert caught.value.status == "rate_limited"
        assert caught.value.retryable is True
        assert len(http_fetcher._DOMAIN_READY) <= http_fetcher._MAX_DOMAINS
    finally:
        with http_fetcher._THROTTLE_LOCK:
            for host in names:
                http_fetcher._DOMAIN_READY.pop(host, None)


def test_request_policy_runs_before_each_redirect_and_can_stop_network(network):
    def respond(handler):
        reply(handler, status=302, headers={"Location": "http://articles.example.org/forbidden"})

    network.serve(respond)
    seen = []

    def policy(url, deadline):
        seen.append(url)
        if url.endswith("/forbidden"):
            raise FetchError("blocked", "Publisher policy disallows this page.")
        return 0

    with pytest.raises(FetchError, match="Publisher policy"):
        SafeFetcher(config()).fetch("http://articles.example.org/start", before_request=policy)
    assert seen == ["http://articles.example.org/start", "http://articles.example.org/forbidden"]
    assert len(network.records) == 1
