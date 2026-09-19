from __future__ import annotations

import contextlib
import json
import socketserver
import threading
import time
from types import SimpleNamespace

import pytest

from telegram_bot.browser_reader import BrowserReader, BrowserReadError

TOKEN = "local-test-token-not-a-secret-123456"
URL = "https://example.com/article?private-query=never-echo"


@contextlib.contextmanager
def endpoint(reply, *, delay=0, trickle=False):
    seen = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = self.request.recv(8192)
                if not chunk:
                    return
                raw += chunk
            header, body = raw.split(b"\r\n\r\n", 1)
            length = int(
                next(
                    x for x in header.split(b"\r\n") if x.lower().startswith(b"content-length:")
                ).split(b":")[1]
            )
            while len(body) < length:
                body += self.request.recv(8192)
            seen.append((header, json.loads(body)))
            time.sleep(delay)
            with contextlib.suppress(OSError):
                wire = reply
                if trickle == "body":
                    header, wire = reply.split(b"\r\n\r\n", 1)
                    self.request.sendall(header + b"\r\n\r\n")
                if trickle:
                    for byte in wire:
                        self.request.sendall(bytes([byte]))
                        time.sleep(0.02)
                else:
                    self.request.sendall(wire)

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        until = time.monotonic() + 1
        while time.monotonic() < until:
            if BrowserReader._slot.acquire(blocking=False):
                BrowserReader._slot.release()
                break
            time.sleep(0.01)


def config(endpoint_url, **changes):
    values = dict(
        browser_enabled=True,
        browser_url=endpoint_url,
        browser_token=TOKEN,
        browser_timeout_seconds=2,
        browser_max_concurrency=1,
        max_response_bytes=1024,
    )
    return SimpleNamespace(**(values | changes))


def response(body=b"<html>Article</html>", status=200, content_type="text/html", extras=b""):
    return (
        f"HTTP/1.1 {status} Test\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n".encode()
        + extras
        + b"\r\n"
        + body
    )


def test_real_http_contract_no_environment_proxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    with endpoint(response()) as (address, seen):
        assert (
            BrowserReader(config(address)).render(URL, deadline=time.monotonic() + 2)
            == b"<html>Article</html>"
        )
    headers, payload = seen[0]
    assert headers.startswith(b"POST /render HTTP/1.1")
    assert f"X-Render-Token: {TOKEN}".encode() in headers
    assert payload["url"] == URL
    assert payload["max_bytes"] == 1024
    assert 0 < payload["timeout_seconds"] <= 2


@pytest.mark.parametrize(
    "code", ["blocked", "too_large", "busy", "rate_limited", "timeout", "invalid"]
)
def test_errors_are_fixed_and_safe(code):
    with endpoint(response(json.dumps({"status": code}).encode(), 422, "application/json")) as (
        address,
        _,
    ):
        with pytest.raises(BrowserReadError) as exc:
            BrowserReader(config(address)).render(URL, deadline=time.monotonic() + 2)
    assert TOKEN not in str(exc.value)
    assert "example.com" not in str(exc.value)
    if code == "blocked":
        assert "challenge" in str(exc.value)


@pytest.mark.parametrize(
    "raw",
    [
        response(b"x" * 1025),
        response(content_type="application/json"),
        response(extras=b"Content-Encoding: gzip\r\n"),
        response(extras=b"Transfer-Encoding: chunked\r\n"),
        response(extras=b"Content-Length: 1\r\n"),
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\nx",
        b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nContent-Type: text/html\r\n\r\nshort",
        b"HTTP/1.1 200 OK\r\nX-Filler: " + b"x" * 9000 + b"\r\n\r\n",
        response(b""),
        response(b'{"status": "https://private.example/secret"}', 500, "application/json"),
    ],
)
def test_rejects_unbounded_ambiguous_or_wrong_responses(raw):
    with endpoint(raw) as (address, _):
        with pytest.raises(BrowserReadError) as exc:
            BrowserReader(config(address)).render(URL, deadline=time.monotonic() + 2)
    assert "private.example" not in str(exc.value)


def test_redirect_not_followed_and_token_not_forwarded():
    with endpoint(response()) as (destination, target_seen):
        with endpoint(
            response(b"", 302, extras=f"Location: {destination}/secret\r\n".encode())
        ) as (address, _):
            with pytest.raises(BrowserReadError):
                BrowserReader(config(address)).render(URL, deadline=time.monotonic() + 2)
        assert target_seen == []


@pytest.mark.parametrize("trickle", [False, True, "body"])
def test_absolute_deadline_covers_slow_headers_and_body(trickle):
    with endpoint(response(), delay=0 if trickle else 0.5, trickle=trickle) as (address, _):
        start = time.monotonic()
        with pytest.raises(BrowserReadError, match="timed out"):
            BrowserReader(config(address)).render(URL, deadline=start + 0.12)
        assert time.monotonic() - start < 0.4


def test_disabled_expired_and_bad_token_do_not_contact_service():
    with endpoint(response()) as (address, seen):
        for settings, deadline in [
            ({"browser_enabled": False}, time.monotonic() + 2),
            ({}, time.monotonic() - 1),
            ({"browser_token": None}, time.monotonic() + 2),
            ({"browser_token": TOKEN + "\r\nx: y"}, time.monotonic() + 2),
            ({"browser_max_concurrency": 2}, time.monotonic() + 2),
            ({"browser_url": address + "/redirect"}, time.monotonic() + 2),
        ]:
            with pytest.raises(BrowserReadError):
                BrowserReader(config(address, **settings)).render(URL, deadline=deadline)
        assert not seen


def test_single_outstanding_request_across_instances():
    with endpoint(response(), delay=0.3) as (address, seen):
        results = []
        first = threading.Thread(
            target=lambda: results.append(
                BrowserReader(config(address)).render(URL, deadline=time.monotonic() + 2)
            )
        )
        first.start()
        until = time.monotonic() + 1
        while not seen and time.monotonic() < until:
            time.sleep(0.01)
        with pytest.raises(BrowserReadError, match="busy"):
            BrowserReader(config(address)).render(URL, deadline=time.monotonic() + 2)
        first.join(2)
        assert len(results) == 1
