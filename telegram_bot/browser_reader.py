"""Synchronous, deadline-bounded client for the optional isolated renderer."""

from __future__ import annotations

import asyncio
import json
import math
import queue
import ssl
import threading
import time
from urllib.parse import urlsplit


class BrowserReadError(RuntimeError):
    """A deliberately URL/token-free error suitable for the scraping workflow."""


_ERRORS = {
    "blocked": "Browser rendering stopped at a challenge, login, or blocked destination.",
    "too_large": "Rendered article exceeded the size limit.",
    "timeout": "Browser rendering timed out.",
    "busy": "Browser renderer is busy.",
    "rate_limited": "Browser renderer quota exceeded.",
}
_MAX_BYTES = 5 * 1024 * 1024


class BrowserReader:
    # Bound outstanding DNS work as well as browser requests across client instances.
    _slot = threading.BoundedSemaphore(1)

    def __init__(self, config):
        self.config = config

    def render(self, url: str, *, deadline: float) -> bytes:
        if not self.config.browser_enabled:
            raise BrowserReadError("Browser rendering is disabled.")
        if not self._slot.acquire(blocking=False):
            raise BrowserReadError(_ERRORS["busy"])
        try:
            remaining = min(float(self.config.browser_timeout_seconds), deadline - time.monotonic())
            if not math.isfinite(remaining) or remaining <= 0:
                raise BrowserReadError(_ERRORS["timeout"])
        except Exception:
            self._slot.release()
            raise BrowserReadError(_ERRORS["timeout"]) from None
        result = queue.Queue(maxsize=1)
        end = time.monotonic() + remaining

        def execute():
            try:
                result.put(asyncio.run(self._render(url, max(0.001, end - time.monotonic()))))
            except BrowserReadError as exc:
                result.put(exc)
            except TimeoutError:
                result.put(BrowserReadError(_ERRORS["timeout"]))
            except Exception:
                result.put(BrowserReadError("Browser renderer is unavailable."))
            finally:
                self._slot.release()

        # asyncio.run may wait for an uncancellable OS DNS lookup during shutdown.
        # Keep that worker bounded to one, without extending the caller's deadline.
        try:
            threading.Thread(target=execute, daemon=True, name="browser-reader").start()
        except Exception:
            self._slot.release()
            raise BrowserReadError("Browser renderer is unavailable.") from None
        try:
            outcome = result.get(timeout=max(0, end - time.monotonic()))
        except queue.Empty:
            raise BrowserReadError(_ERRORS["timeout"]) from None
        if isinstance(outcome, BrowserReadError):
            raise outcome
        return outcome

    async def _render(self, url: str, remaining: float) -> bytes:
        endpoint = urlsplit(self.config.browser_url)
        token = self.config.browser_token
        max_bytes = self.config.max_response_bytes
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
            or not isinstance(token, str)
            or not 32 <= len(token) <= 256
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= _MAX_BYTES
            or self.config.browser_max_concurrency != 1
            or not isinstance(url, str)
        ):
            raise BrowserReadError("Browser renderer configuration is invalid.")
        payload = json.dumps(
            {"url": url, "max_bytes": max_bytes, "timeout_seconds": min(25.0, remaining)},
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        if len(payload) > 8192:
            raise BrowserReadError("Browser render request is invalid.")
        host = endpoint.hostname.encode("idna").decode("ascii")
        port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
        authority = f"[{host}]" if ":" in host else host
        request = (
            f"POST /render HTTP/1.1\r\nHost: {authority}:{port}\r\n"
            f"X-Render-Token: {token}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
        ).encode("ascii") + payload
        writer = None
        # A single timeout covers DNS, TCP/TLS, headers and the entire body (including trickles).
        async with asyncio.timeout(remaining):
            try:
                reader, writer = await asyncio.open_connection(
                    host,
                    port,
                    ssl=ssl.create_default_context() if endpoint.scheme == "https" else None,
                    limit=8192,
                )
                writer.write(request)
                await writer.drain()
                raw_headers = await reader.readuntil(b"\r\n\r\n")
                if len(raw_headers) > 8192:
                    raise ValueError("headers")
                lines = raw_headers[:-4].decode("ascii").split("\r\n")
                version, status, _ = lines[0].split(" ", 2)
                if version not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise ValueError("version")
                headers = {}
                for line in lines[1:]:
                    key, value = line.split(":", 1)
                    if not key or key != key.strip() or key.lower() in headers:
                        raise ValueError("headers")
                    headers[key.lower()] = value.strip()
                length = headers.get("content-length", "")
                if (
                    not length.isascii()
                    or not length.isdecimal()
                    or "transfer-encoding" in headers
                    or headers.get("content-encoding", "identity") != "identity"
                ):
                    raise ValueError("framing")
                size = int(length)
                if size > (max_bytes if status == "200" else 256):
                    raise BrowserReadError(_ERRORS["too_large"])
                content_type = headers.get("content-type", "").split(";", 1)[0].lower()
                if status == "200" and content_type != "text/html":
                    raise ValueError("content type")
                body = await reader.readexactly(size)
                if status != "200":
                    code = None
                    if content_type == "application/json":
                        error = json.loads(body)
                        if isinstance(error, dict) and set(error) == {"status"}:
                            code = error["status"]
                    raise BrowserReadError(
                        _ERRORS.get(code, "Browser renderer could not render this article.")
                    )
                if not body:
                    raise ValueError("empty response")
                return body
            finally:
                if writer is not None:
                    writer.close()
                    # Do not wait for a remote TLS close-notify beyond the shared deadline.
