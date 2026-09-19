from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from browser_service import app
from browser_service import egress_proxy as proxy
from telegram_bot.browser_reader import BrowserReader

TOKEN = "local-test-token-not-a-secret-123456"
PAYLOAD = {"url": "https://example.com/article", "max_bytes": 4096, "timeout_seconds": 5}


def request(body=None, *, token=TOKEN, extra=b"", target="/render", method="POST"):
    body = json.dumps(PAYLOAD).encode() if body is None else body
    return (
        f"{method} {target} HTTP/1.1\r\nHost: renderer\r\nX-Render-Token: {token}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n".encode()
        + extra
        + b"\r\n"
        + body
    )


async def exchange(port, raw):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(raw)
        await writer.drain()
        async with asyncio.timeout(4):
            wire = await reader.read()
        header, body = wire.split(b"\r\n\r\n", 1)
        return int(header.split(b" ")[1]), body
    finally:
        writer.close()
        await writer.wait_closed()


@contextlib.asynccontextmanager
async def service(renderer):
    # Inject only the unavailable/expensive browser boundary; use real HTTP and policy paths.
    instance = app.RenderService(TOKEN, renderer=renderer)
    server = await instance.start("127.0.0.1", 0)
    try:
        yield instance, server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()
        await instance.close()


async def html_renderer(payload):
    return b"<html><body>Rendered article</body></html>"


def test_real_service_contract():
    async def run():
        async with service(html_renderer) as (_, port):
            status, body = await exchange(port, request())
            assert status == 200
            assert body == await html_renderer(PAYLOAD)

    asyncio.run(run())


@pytest.mark.parametrize(
    "raw, status, code",
    [
        (request(token="wrong"), 401, "unauthorized"),
        (request(extra=f"X-Render-Token: {TOKEN}\r\n".encode()), 400, "invalid"),
        (request(extra=b"Transfer-Encoding: chunked\r\n"), 400, "invalid"),
        (request(extra=b"Content-Encoding: gzip\r\n"), 400, "invalid"),
        (request(extra=b"Content-Length: 0\r\n"), 400, "invalid"),
        (request(extra=b"Expect: 100-continue\r\n"), 400, "invalid"),
        (request(target="/render?url=secret"), 400, "invalid"),
        (request(target="https://example.com/render"), 400, "invalid"),
        (request(method="GET"), 400, "invalid"),
        (request(body=b"[]"), 400, "invalid"),
        (request(body=b"{"), 400, "invalid"),
        (
            request(body=json.dumps(PAYLOAD | {"url": "http://127.0.0.1/secret"}).encode()),
            422,
            "blocked",
        ),
        (request(body=json.dumps(PAYLOAD | {"extra": "secret"}).encode()), 400, "invalid"),
        (request(body=b"x" * 8193), 400, "invalid"),
    ],
)
def test_request_policy_fails_closed_without_launching_browser(raw, status, code):
    async def never(payload):
        raise AssertionError("browser must not run")

    async def run():
        async with service(never) as (instance, port):
            actual, body = await exchange(port, raw)
            assert (actual, json.loads(body)) == (status, {"status": code})
            assert b"secret" not in body and TOKEN.encode() not in body
            assert not instance.policy.renders

    asyncio.run(run())


@pytest.mark.parametrize(
    "changes",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": 26},
        {"timeout_seconds": True},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"max_bytes": 0},
        {"max_bytes": app.MAX_BYTES + 1},
        {"max_bytes": True},
        {"url": None},
    ],
)
def test_payload_bounds(changes):
    with pytest.raises(app.RenderFailure):
        app.parse_payload(json.dumps(PAYLOAD | changes).encode())


def test_duplicate_json_keys_rejected():
    with pytest.raises(app.RenderFailure, match="invalid"):
        app.parse_payload(
            b'{"url":"https://example.com","url":"https://example.org","max_bytes":1,"timeout_seconds":1}'
        )


@pytest.mark.parametrize("token", [None, "", "short", "x" * 257, "x" * 32 + "\r\n"])
def test_missing_or_bad_token_prevents_startup(token):
    with pytest.raises(ValueError):
        app.RenderService(token)


def test_service_concurrency_and_render_quota():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(payload):
            entered.set()
            await release.wait()
            return await html_renderer(payload)

        async with service(slow) as (instance, port):
            first = asyncio.create_task(exchange(port, request()))
            await entered.wait()
            assert await exchange(port, request()) == (429, b'{"status": "busy"}')
            release.set()
            assert (await first)[0] == 200
            instance.renderer = html_renderer
            for _ in range(app.RENDERS_PER_MINUTE - 1):
                assert (await exchange(port, request()))[0] == 200
            assert await exchange(port, request()) == (429, b'{"status": "rate_limited"}')
            assert not instance.policy.active

    asyncio.run(run())


def test_global_request_quota_includes_failed_authentication():
    async def run():
        async with service(html_renderer) as (_, port):
            for _ in range(app.REQUESTS_PER_MINUTE):
                assert (await exchange(port, request(token="bad")))[0] == 401
            assert await exchange(port, request()) == (429, b'{"status": "rate_limited"}')

    asyncio.run(run())


def test_hour_quota_and_window_expiry():
    policy = app.RequestPolicy()
    policy.renders.extend([time.monotonic() - 61] * app.RENDERS_PER_HOUR)
    with pytest.raises(app.RenderFailure, match="rate_limited"):
        policy.reserve()
    policy.renders.clear()
    policy.renders.extend([time.monotonic() - 3601] * app.RENDERS_PER_HOUR)
    policy.reserve()
    assert policy.active and len(policy.renders) == 1


def test_service_header_timeout_and_connection_bound():
    async def run():
        async with service(html_renderer) as (instance, port):
            connections = [await asyncio.open_connection("127.0.0.1", port) for _ in range(4)]
            await asyncio.sleep(0.03)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            assert await asyncio.wait_for(reader.read(), 0.5) == b""
            writer.close()
            started = time.monotonic()
            wire = await asyncio.wait_for(connections[0][0].read(), 2.5)
            assert b'"timeout"' in wire
            assert time.monotonic() - started < 2.5
            for _, writer in connections:
                writer.close()
            await asyncio.sleep(0.05)
            assert not instance.policy.active

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["exception", "too_large", "deadline"])
def test_safe_errors_and_release_after_worker_failures(kind):
    async def renderer(payload):
        if kind == "exception":
            raise RuntimeError("https://private.example/secret token=secret")
        if kind == "too_large":
            return b"x" * (payload["max_bytes"] + 1)
        await asyncio.sleep(3)

    async def run():
        async with service(renderer) as (instance, port):
            status, body = await exchange(
                port, request(body=json.dumps(PAYLOAD | {"timeout_seconds": 1}).encode())
            )
            assert status in {413, 503, 504}
            assert b"secret" not in body
            assert not instance.policy.active

    asyncio.run(run())


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "10.1.2.3",
        "127.0.0.1",
        "169.254.169.254",
        "169.254.170.2",
        "168.63.129.16",
        "172.16.0.1",
        "192.168.0.1",
        "100.64.0.1",
        "192.0.0.9",
        "192.0.2.1",
        "192.88.99.1",
        "198.18.0.1",
        "198.51.100.1",
        "203.0.113.1",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "fd00::1",
        "fe80::1",
        "ff02::1",
        "::ffff:8.8.8.8",
        "64:ff9b::808:808",
        "2002:0808:0808::1",
        "2001::1",
        "2001:db8::1",
        "3fff::1",
    ],
)
def test_denies_private_reserved_metadata_and_transition_addresses(address):
    with pytest.raises(proxy.PolicyError):
        proxy.public_ip(address)


@pytest.mark.parametrize(
    "address",
    ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2001:4860:4860::8888", "2606:4700:4700::1111"],
)
def test_public_addresses_allowed(address):
    assert str(proxy.public_ip(address)) == address


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/html,hello",
        "ftp://example.com",
        "ws://example.com/",
        "wss://example.com/",
        "https://user:pass@example.com/",
        "https://user@example.com/",
        "https://example.com:8080/",
        "https://example.com:0/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "http://localhost/",
        "http://localhost./",
        "http://2130706433/",
        "http://127.1/",
        "http://a.internal/",
        "http://example.com\\@127.0.0.1/",
        "http://example.com/%0a\r\nX: hi",
        "http://[fe80::1%25eth0]/",
        "http://%31%32%37.0.0.1/",
    ],
)
def test_url_policy(url):
    with pytest.raises(proxy.PolicyError):
        proxy.validate_public_url(url)


def test_real_os_resolution_rejects_localhost_aliases():
    async def run():
        for host in ("localhost", "127.1", "2130706433", "0x7f000001"):
            with pytest.raises(proxy.PolicyError):
                await proxy.resolve_public_addresses(host, 80)

    asyncio.run(run())


def test_dns_mixed_answers_and_rebinding_are_rejected(monkeypatch):
    async def run():
        # Deterministic DNS answers are necessary to exercise rebinding without external DNS.
        def mixed(*args, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
            ]

        monkeypatch.setattr(proxy.socket, "getaddrinfo", mixed)
        with pytest.raises(proxy.PolicyError):
            await proxy.resolve_public_addresses("example.com", 443)

    asyncio.run(run())


def test_connections_use_numeric_checked_ip_never_a_second_dns_lookup(monkeypatch):
    async def run():
        loop = asyncio.get_running_loop()
        resolutions, connections = [], []

        def dns(host, port, *args, **kwargs):
            resolutions.append(host)
            address = "8.8.8.8" if len(resolutions) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

        async def connect(sock, address):
            connections.append(address)
            raise OSError("network intentionally not contacted")

        # Substitute only OS network boundaries; exercise real resolution/validation/connect code.
        monkeypatch.setattr(proxy.socket, "getaddrinfo", dns)
        monkeypatch.setattr(loop, "sock_connect", connect)
        with pytest.raises(proxy.PolicyError):
            await proxy.connect_public("example.com", 443)
        assert resolutions == ["example.com"]
        assert connections == [("8.8.8.8", 443)]

    asyncio.run(run())


@pytest.mark.parametrize(
    "raw",
    [
        b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n",
        b"CONNECT example.com:80 HTTP/1.1\r\nHost: example.com:80\r\n\r\n",
        b"CONNECT user:secret@example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n",
        b"GET http://example.com/ HTTP/1.1\r\nHost: internal.local\r\n\r\n",
        b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nContent-Length: 1\r\n\r\n",
        b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nUpgrade: websocket\r\n\r\n",
        b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nAuthorization: Basic secret\r\n\r\n",
        b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nCookie: secret\r\n\r\n",
        b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nHost: example.com\r\n\r\n",
        b"GET file:///etc/passwd HTTP/1.1\r\nHost: example.com\r\n\r\n",
        b"GET /relative HTTP/1.1\r\nHost: example.com\r\n\r\n",
        b"POST http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n",
    ],
)
def test_proxy_rejects_unsafe_hops_framing_and_credentials_over_real_socket(raw):
    async def run():
        instance = proxy.EgressProxy()
        server = await asyncio.start_server(
            instance.handle, "127.0.0.1", 0, limit=proxy.HEADER_LIMIT
        )
        try:
            status, body = await exchange(server.sockets[0].getsockname()[1], raw)
            assert status == 403 and body == b""
        finally:
            server.close()
            await server.wait_closed()
            await instance.close()

    asyncio.run(run())


def test_proxy_only_forwards_safe_headers_and_canonical_host():
    method, host, port, raw = proxy.parse_request(
        b"GET http://EXAMPLE.com/path?q=1 HTTP/1.1\r\nHost: example.com\r\n"
        b"X-Render-Token: secret\r\nX-Forwarded-For: 127.0.0.1\r\n\r\n"
    )
    assert (method, host, port) == ("GET", "example.com", 80)
    assert b"GET /path?q=1 HTTP/1.1" in raw
    assert b"secret" not in raw and b"X-Forwarded" not in raw
    assert b"Connection: close" in raw
    assert proxy.parse_request(
        b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
    )[:3] == ("CONNECT", "example.com", 443)


def test_proxy_tunnel_byte_limit_and_concurrency(monkeypatch):
    async def run():
        released = asyncio.Event()

        async def origin(reader, writer):
            await released.wait()
            writer.write(b"x" * 2048)
            with contextlib.suppress(OSError):
                await writer.drain()
            writer.close()

        upstream = await asyncio.start_server(origin, "127.0.0.1", 0)
        port = upstream.sockets[0].getsockname()[1]

        async def controlled_transport(host, destination_port):
            return await asyncio.open_connection("127.0.0.1", port)

        # Test-only transport routes an otherwise public CONNECT to a controlled TCP peer.
        monkeypatch.setattr(proxy, "connect_public", controlled_transport)
        monkeypatch.setattr(proxy, "CONNECTION_BYTES", 1024)
        monkeypatch.setattr(proxy, "CONNECTION_LIMIT", 1)
        instance = proxy.EgressProxy()
        server = await asyncio.start_server(instance.handle, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.sockets[0].getsockname()[1]
        )
        try:
            writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
            await writer.drain()
            assert b"200" in await reader.readuntil(b"\r\n\r\n")
            other, other_writer = await asyncio.open_connection(
                "127.0.0.1", server.sockets[0].getsockname()[1]
            )
            assert await asyncio.wait_for(other.read(), 0.5) == b""
            other_writer.close()
            released.set()
            assert await asyncio.wait_for(reader.read(), 1) == b""
        finally:
            writer.close()
            server.close()
            upstream.close()
            await instance.close()
            await server.wait_closed()
            await upstream.wait_closed()

    asyncio.run(run())


@pytest.mark.parametrize(
    "html",
    [
        "<title>Just a moment...</title>",
        "<script src='/cdn-cgi/challenge-platform/a.js'></script>",
        "<div class='cf-turnstile'></div>",
        "<div>Verify you are human</div>",
        "<form><input TYPE='PASSWORD'></form>",
        "<p>Sign in to continue</p>",
    ],
)
def test_challenge_and_login_detection(html):
    assert app.blocked_html(html)


def test_normal_article_not_classified_as_challenge():
    assert not app.blocked_html(
        "<article><h1>Research update</h1><p>Legitimate article.</p></article>"
    )


def test_browser_environment_does_not_inherit_tokens_proxies_or_bot_secrets(monkeypatch):
    for name in (
        "RENDER_TOKEN",
        "SCRAPER_BROWSER_TOKEN",
        "OPENAI_API_KEY",
        "TELEGRAM_TOKEN",
        "HTTP_PROXY",
        "LD_PRELOAD",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, "secret")
    assert set(app.worker_environment()) <= app.CHILD_ENV_KEYS
    assert "secret" not in app.worker_environment().values()


def test_worker_real_subprocess_rejects_blocked_url_before_browser_launch():
    async def run():
        with pytest.raises(app.RenderFailure, match="blocked"):
            await app.run_worker(PAYLOAD | {"url": "http://127.0.0.1/"})

    asyncio.run(run())


def test_worker_deadline_kills_real_process_group(monkeypatch):
    async def run():
        original = asyncio.create_subprocess_exec
        pids = []

        async def controlled_process(*args, **kwargs):
            process = await original(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
            pids.append(process.pid)
            return process

        # A real sleeping process exercises the watchdog without installing browser binaries.
        monkeypatch.setattr(asyncio, "create_subprocess_exec", controlled_process)
        started = time.monotonic()
        with pytest.raises(app.RenderFailure, match="timeout"):
            await app.run_worker(PAYLOAD | {"timeout_seconds": 1.2})
        assert time.monotonic() - started < 1.2
        with pytest.raises(ProcessLookupError):
            os.kill(pids[0], 0)

    asyncio.run(run())


def test_compose_isolation_and_clean_image_context():
    yaml = pytest.importorskip("yaml")
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.browser.yml").read_text())
    services = compose["services"]
    assert set(services["linkedin-bot"]["networks"]) == {"default", "renderer"}
    assert services["scraper-browser"]["networks"] == ["renderer"]
    assert set(services["scraper-egress"]["networks"]) == {"renderer", "renderer-egress"}
    assert compose["networks"]["renderer"]["internal"] is True
    for name in ("scraper-browser", "scraper-egress"):
        spec = services[name]
        assert not any(
            key in spec for key in ("ports", "env_file", "volumes", "privileged", "network_mode")
        )
        assert spec["read_only"] and spec["cap_drop"] == ["ALL"]
        assert spec["build"]["context"] == "./browser_service"
    assert set(services["scraper-browser"]["environment"]) == {"RENDER_TOKEN"}
    assert (
        "COPY --chmod=0644 app.py egress_proxy.py"
        in (root / "browser_service/Dockerfile").read_text()
    )
    assert (
        "COPY --chmod=0644 egress_proxy.py"
        in (root / "browser_service/Dockerfile.proxy").read_text()
    )
    assert "playwright==1.63.0" in (root / "browser_service/requirements.txt").read_text()
    assert ":v1.63.0-noble" in (root / "browser_service/Dockerfile").read_text()


@pytest.mark.parametrize("challenge", [False, True])
def test_real_controlled_javascript_when_playwright_available(monkeypatch, challenge):
    if importlib.util.find_spec("playwright") is None:
        pytest.skip(
            "Playwright Python dependency is unavailable; coordinate installation with parent"
        )
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():
                pytest.skip("Matching Playwright browser binary is unavailable")
        article = "Controlled JavaScript article content. " * 12
        html = (
            "<html><body><main id='article'></main><script>"
            "setTimeout(() => document.getElementById('article').textContent = "
            + json.dumps(article)
            + ", 100);</script></body></html>"
        ).encode()
        if challenge:
            html = b"<html><title>Just a moment...</title><body>Checking your browser</body></html>"

        async def fixture_proxy(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                + str(len(html)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + html
            )
            await writer.drain()
            writer.close()

        # Only the remote HTTP transport is controlled; Chromium and render policy are real.
        server = await asyncio.start_server(fixture_proxy, "127.0.0.1", 0)
        monkeypatch.setattr(
            app, "PROXY_URL", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        )
        try:
            async with asyncio.timeout(20):
                if challenge:
                    with pytest.raises(app.RenderFailure, match="blocked"):
                        await app.render_page(PAYLOAD | {"url": "http://example.com/article"})
                else:
                    rendered = await app.render_page(
                        PAYLOAD | {"url": "http://example.com/article"}
                    )
                    assert ('<main id="article">' + article).encode() in rendered
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_dns_timeout_does_not_release_real_lookup_capacity(monkeypatch):
    release = threading.Event()
    entered = threading.Barrier(5)

    def stalled_dns(*args, **kwargs):
        entered.wait(timeout=2)
        release.wait(timeout=2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    async def run():
        monkeypatch.setattr(proxy.socket, "getaddrinfo", stalled_dns)
        monkeypatch.setattr(proxy, "IDLE_SECONDS", 0.1)
        tasks = [
            asyncio.create_task(proxy.resolve_public_addresses("example.com", 443))
            for _ in range(4)
        ]
        try:
            await asyncio.to_thread(entered.wait, 2)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(result, TimeoutError) for result in results)
            with pytest.raises(proxy.PolicyError):
                await proxy.resolve_public_addresses("example.com", 443)
        finally:
            release.set()
            await asyncio.sleep(0.05)

    asyncio.run(run())


def test_proxy_header_and_absolute_connection_deadlines(monkeypatch):
    async def run():
        monkeypatch.setattr(proxy, "IDLE_SECONDS", 0.1)
        monkeypatch.setattr(proxy, "CONNECTION_SECONDS", 0.2)
        instance = proxy.EgressProxy()
        server = await asyncio.start_server(
            instance.handle, "127.0.0.1", 0, limit=proxy.HEADER_LIMIT
        )
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.sockets[0].getsockname()[1]
        )
        try:
            writer.write(b"GET http://example.com/ HTTP/1.1\r\n")
            await writer.drain()
            started = time.monotonic()
            wire = await asyncio.wait_for(reader.read(), 0.5)
            assert b"403" in wire and time.monotonic() - started < 0.5
            assert instance.active == 0
        finally:
            writer.close()
            server.close()
            await server.wait_closed()
            await instance.close()

    asyncio.run(run())


def test_proxy_connection_and_byte_window_quotas():
    async def run():
        for quota in ("connections", "transferred"):
            instance = proxy.EgressProxy()
            setattr(
                instance,
                quota,
                proxy.CONNECTIONS_PER_MINUTE if quota == "connections" else proxy.BYTES_PER_MINUTE,
            )
            server = await asyncio.start_server(instance.handle, "127.0.0.1", 0)
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", server.sockets[0].getsockname()[1]
            )
            try:
                assert await asyncio.wait_for(reader.read(), 0.5) == b""
                assert instance.active == 0
                instance.window = time.monotonic() - 61
                instance._roll_window()
                assert instance.connections == instance.transferred == 0
            finally:
                writer.close()
                server.close()
                await server.wait_closed()
                await instance.close()

    asyncio.run(run())


def test_browser_client_and_service_interoperate_over_real_http():
    async def run():
        async with service(html_renderer) as (_, port):
            config = SimpleNamespace(
                browser_enabled=True,
                browser_url=f"http://127.0.0.1:{port}",
                browser_token=TOKEN,
                browser_timeout_seconds=5,
                browser_max_concurrency=1,
                max_response_bytes=4096,
            )
            result = await asyncio.to_thread(
                BrowserReader(config).render,
                PAYLOAD["url"],
                deadline=time.monotonic() + 5,
            )
            assert result == await html_renderer(PAYLOAD)

    asyncio.run(run())


def test_rendered_document_identity_cannot_silently_change():
    assert app.same_document("https://example.com/article#part", "https://example.com/article")
    assert not app.same_document("https://example.com/unrelated", "https://example.com/article")
    assert not app.same_document("https://other.example.com/article", "https://example.com/article")
    assert not app.same_document(
        "https://example.com/article?other=1", "https://example.com/article"
    )
