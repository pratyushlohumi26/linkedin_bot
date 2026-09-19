"""Authenticated, single-render service. No scraping credentials enter its worker."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import math
import os
import re
import signal
import sys
import time
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar

from browser_service.egress_proxy import PolicyError, validate_public_url

MAX_BYTES = 5 * 1024 * 1024
MAX_REQUEST = 8192
MAX_SECONDS = 25
REQUESTS_PER_MINUTE = 30
RENDERS_PER_MINUTE = 6
RENDERS_PER_HOUR = 60
PROXY_URL = "http://scraper-egress:8899"
RESOURCE_LIMIT = 60
RESOURCE_BYTES = 2 * 1024 * 1024
TOTAL_RESOURCE_BYTES = 20 * 1024 * 1024
CHILD_ENV_KEYS = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PLAYWRIGHT_BROWSERS_PATH"}
STATUS_CODES = {
    "invalid": 400,
    "unauthorized": 401,
    "blocked": 422,
    "too_large": 413,
    "busy": 429,
    "rate_limited": 429,
    "timeout": 504,
    "unavailable": 503,
}
EXIT_CODES = {"blocked": 20, "too_large": 21, "timeout": 22, "unavailable": 23}
_CHALLENGE = re.compile(
    r"cf-chl-|/cdn-cgi/challenge-platform|cf-turnstile|g-recaptcha|h-captcha|"
    r"verify (?:that )?you are human|checking your browser|just a moment|"
    r"enable javascript and cookies to continue|attention required!.*cloudflare|"
    r"unusual traffic from your|access denied|sign in to continue|log in to continue",
    re.IGNORECASE | re.DOTALL,
)
_LOGIN_PATH = re.compile(
    r"/(?:login|log-in|signin|sign-in|oauth|authorize|checkpoint)(?:[/?#]|$)", re.I
)


class RenderFailure(Exception):
    def __init__(self, status: str):
        self.status = status if status in STATUS_CODES else "unavailable"
        super().__init__(self.status)


class LoginDetector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and (attrs.get("type") or "").lower() == "password":
            self.found = True


def blocked_html(html: str) -> bool:
    detector = LoginDetector()
    detector.feed(html)
    return detector.found or bool(_CHALLENGE.search(html))


def same_document(url: str, original: str) -> bool:
    return urlsplit(url).scheme == urlsplit(original).scheme and validate_public_url(
        url
    ) == validate_public_url(original)


def parse_payload(raw: bytes) -> dict:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        payload = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(payload, dict) or set(payload) != {"url", "max_bytes", "timeout_seconds"}:
            raise ValueError("fields")
        validate_public_url(payload["url"])
        if _LOGIN_PATH.search(payload["url"]):
            raise RenderFailure("blocked")
        timeout = payload["timeout_seconds"]
        if (
            type(payload["max_bytes"]) is not int
            or not 1 <= payload["max_bytes"] <= MAX_BYTES
            or type(timeout) not in {int, float}
            or not math.isfinite(timeout)
            or not 1 <= timeout <= MAX_SECONDS
        ):
            raise ValueError("bounds")
        return payload
    except PolicyError:
        raise RenderFailure("blocked") from None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RenderFailure("invalid") from None


def worker_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in CHILD_ENV_KEYS}


async def run_worker(payload: dict) -> bytes:
    process = None
    try:
        # Reserve one second of the caller's budget for forced process-group teardown.
        async with asyncio.timeout(max(0.1, payload["timeout_seconds"] - 1)):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "browser_service.app",
                "--worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=worker_environment(),
                start_new_session=True,
                limit=65536,
            )
            process.stdin.write(json.dumps(payload).encode("ascii"))
            await process.stdin.drain()
            process.stdin.close()
            chunks = []
            size = 0
            while chunk := await process.stdout.read(min(65536, payload["max_bytes"] + 1 - size)):
                size += len(chunk)
                if size > payload["max_bytes"]:
                    raise RenderFailure("too_large")
                chunks.append(chunk)
            code = await process.wait()
            if code != 0:
                status = next(
                    (key for key, value in EXIT_CODES.items() if value == code), "unavailable"
                )
                raise RenderFailure(status)
            if not size:
                raise RenderFailure("unavailable")
            return b"".join(chunks)
    except TimeoutError:
        raise RenderFailure("timeout") from None
    finally:
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(1):
                    await process.wait()


class RequestPolicy:
    def __init__(self):
        self.requests = deque()
        self.renders = deque()
        self.active = False

    def request_allowed(self):
        now = time.monotonic()
        while self.requests and self.requests[0] <= now - 60:
            self.requests.popleft()
        if len(self.requests) >= REQUESTS_PER_MINUTE:
            return False
        self.requests.append(now)
        return True

    def reserve(self):
        now = time.monotonic()
        while self.renders and self.renders[0] <= now - 3600:
            self.renders.popleft()
        if self.active:
            raise RenderFailure("busy")
        if (
            len(self.renders) >= RENDERS_PER_HOUR
            or sum(t > now - 60 for t in self.renders) >= RENDERS_PER_MINUTE
        ):
            raise RenderFailure("rate_limited")
        self.renders.append(now)
        self.active = True


def response(status: str) -> tuple[int, bytes]:
    return STATUS_CODES[status], json.dumps({"status": status}).encode("ascii")


class RenderService:
    def __init__(self, token: str, *, renderer=run_worker):
        if (
            not isinstance(token, str)
            or not 32 <= len(token) <= 256
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
        ):
            raise ValueError("A dedicated 32-256 character render token is required.")
        self.token = token
        self.renderer = renderer
        self.policy = RequestPolicy()
        self.tasks = set()

    def parse_headers(self, raw: bytes) -> int:
        if len(raw) > MAX_REQUEST:
            raise RenderFailure("invalid")
        try:
            lines = raw[:-4].decode("ascii").split("\r\n")
            headers = {}
            for line in lines[1:]:
                name, value = line.split(":", 1)
                name = name.lower()
                if (
                    not re.fullmatch(r"[a-z0-9-]+", name)
                    or name in headers
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)
                ):
                    raise ValueError("headers")
                headers[name] = value.strip()
            if not hmac.compare_digest(headers.get("x-render-token", ""), self.token):
                raise RenderFailure("unauthorized")
            length = headers.get("content-length", "")
            if (
                lines[0] != "POST /render HTTP/1.1"
                or headers.get("content-type", "").lower() != "application/json"
                or "host" not in headers
                or any(h in headers for h in ("transfer-encoding", "content-encoding", "expect"))
                or not length.isdecimal()
                or not 1 <= int(length) <= MAX_REQUEST
            ):
                raise ValueError("request")
            return int(length)
        except (ValueError, UnicodeError):
            raise RenderFailure("invalid") from None

    async def handle(self, reader, writer):
        if len(self.tasks) >= 4:
            writer.close()
            return
        task = asyncio.current_task()
        self.tasks.add(task)
        reserved = False
        try:
            allowed = self.policy.request_allowed()
            async with asyncio.timeout(2):
                raw = await reader.readuntil(b"\r\n\r\n")
                if not allowed:
                    raise RenderFailure("rate_limited")
                length = self.parse_headers(raw)
                payload = parse_payload(await reader.readexactly(length))
            self.policy.reserve()
            reserved = True
            async with asyncio.timeout(payload["timeout_seconds"]):
                body = await self.renderer(payload)
            if not isinstance(body, bytes) or not body:
                raise RenderFailure("unavailable")
            if len(body) > payload["max_bytes"]:
                raise RenderFailure("too_large")
            status = 200
        except RenderFailure as exc:
            status, body = response(exc.status)
        except TimeoutError:
            status, body = response("timeout")
        except Exception:
            status, body = response("unavailable")
        finally:
            if reserved:
                self.policy.active = False
            if task.cancelling():
                writer.close()
                self.tasks.discard(task)
        try:
            content_type = "text/html; charset=utf-8" if status == 200 else "application/json"
            writer.write(
                f"HTTP/1.1 {status} Render\r\nContent-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                "Connection: close\r\n\r\n".encode("ascii")
            )
            writer.write(body)
            async with asyncio.timeout(2):
                await writer.drain()
        except (OSError, TimeoutError):
            pass
        finally:
            writer.close()
            self.tasks.discard(task)

    async def start(self, host="0.0.0.0", port=8787):
        return await asyncio.start_server(self.handle, host, port, limit=MAX_REQUEST, backlog=16)

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


async def render_page(payload: dict) -> bytes:
    # Optional dependency remains in the isolated image, not in the Telegram bot.
    from playwright.async_api import async_playwright

    blocked = asyncio.Event()
    failure = ["blocked"]
    resource_count = 0
    total_bytes = 0
    inflight = 0
    browser = None
    context = None
    jobs = []

    def stop(status="blocked"):
        failure[0] = status
        blocked.set()

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                headless=True,
                chromium_sandbox=True,
                proxy={"server": PROXY_URL},
                env=worker_environment(),
                timeout=8000,
                args=[
                    "--disable-quic",
                    "--disable-background-networking",
                    "--disable-extensions",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--proxy-bypass-list=<-loopback>",
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE scraper-egress",
                    "--disable-features=WebRtcHideLocalIpsWithMdns,DnsOverHttps,UseDnsHttpsSvcbAlpn",
                ],
            )
            context = await browser.new_context(
                accept_downloads=False,
                service_workers="block",
                java_script_enabled=True,
                user_agent="PublicArticleFetcher/1.0",
                ignore_https_errors=False,
                viewport={"width": 1280, "height": 720},
            )
            await context.add_init_script(
                """for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'WebTransport']) {
                    Object.defineProperty(globalThis, name, {
                        value: undefined, configurable: false, writable: false
                    });
                }"""
            )
            page = await context.new_page()
            page.on("download", lambda _: stop())
            context.on("page", lambda _: stop())

            def navigation(frame):
                try:
                    validate_public_url(frame.url)
                    if _LOGIN_PATH.search(frame.url) or (
                        frame == page.main_frame and not same_document(frame.url, payload["url"])
                    ):
                        stop()
                except PolicyError:
                    stop()

            page.on("framenavigated", navigation)

            async def block_socket(ws):
                await ws.close()

            await context.route_web_socket("**/*", block_socket)
            async with ClientSession(
                trust_env=False,
                cookie_jar=DummyCookieJar(),
                auto_decompress=False,
                timeout=ClientTimeout(total=6),
            ) as session:

                async def route_resource(route):
                    nonlocal resource_count, total_bytes, inflight
                    request = route.request
                    if request.resource_type in {"image", "media", "font", "texttrack", "manifest"}:
                        await route.abort()
                        return
                    counted = False
                    try:
                        if blocked.is_set():
                            raise RenderFailure(failure[0])
                        validate_public_url(request.url)
                        if (
                            request.method not in {"GET", "HEAD"}
                            or (
                                request.is_navigation_request()
                                and not same_document(request.url, payload["url"])
                            )
                            or request.resource_type in {"websocket", "eventsource"}
                            or (
                                request.is_navigation_request() and request.frame != page.main_frame
                            )
                            or _LOGIN_PATH.search(request.url)
                            or any(
                                k in request.headers
                                for k in ("authorization", "proxy-authorization", "cookie")
                            )
                        ):
                            raise RenderFailure("blocked")
                        resource_count += 1
                        if resource_count > RESOURCE_LIMIT or inflight >= 6:
                            raise RenderFailure("blocked")
                        inflight += 1
                        counted = True
                        headers = {
                            "Accept": request.headers.get("accept", "*/*"),
                            "Accept-Encoding": "identity",
                            "User-Agent": request.headers.get("user-agent", ""),
                        }
                        async with session.request(
                            request.method,
                            request.url,
                            proxy=PROXY_URL,
                            allow_redirects=False,
                            headers=headers,
                        ) as fetched:
                            # Redirects are deliberately unsupported, including within TLS tunnels.
                            if (
                                300 <= fetched.status < 400
                                or fetched.status in {401, 403, 407, 429, 503}
                                or fetched.headers.get("cf-mitigated", "").lower() == "challenge"
                                or "attachment"
                                in fetched.headers.get("Content-Disposition", "").lower()
                                or fetched.headers.get("Content-Encoding", "identity").lower()
                                != "identity"
                            ):
                                raise RenderFailure("blocked")
                            main_document = request.resource_type == "document"
                            content_type = fetched.headers.get("Content-Type", "").lower()
                            if main_document and (
                                fetched.status != 200
                                or content_type.split(";", 1)[0]
                                not in {"text/html", "application/xhtml+xml"}
                            ):
                                raise RenderFailure("blocked")
                            cap = payload["max_bytes"] if main_document else RESOURCE_BYTES
                            if fetched.content_length is not None and fetched.content_length > cap:
                                raise RenderFailure("too_large")
                            body = bytearray()
                            async for chunk in fetched.content.iter_chunked(32768):
                                total_bytes += len(chunk)
                                if (
                                    len(body) + len(chunk) > cap
                                    or total_bytes > TOTAL_RESOURCE_BYTES
                                ):
                                    raise RenderFailure("too_large")
                                body.extend(chunk)
                            if "html" in content_type or main_document:
                                try:
                                    text = body.decode(fetched.charset or "utf-8", errors="strict")
                                except (UnicodeError, LookupError):
                                    raise RenderFailure("blocked") from None
                                if blocked_html(text):
                                    raise RenderFailure("blocked")
                                body = text.encode("utf-8")
                                if len(body) > cap:
                                    raise RenderFailure("too_large")
                            # Drop Set-Cookie, authentication, redirects, refresh and hop-by-hop headers.
                            clean_headers = {
                                key: value
                                for key, value in fetched.headers.items()
                                if key.lower()
                                in {
                                    "content-type",
                                    "content-security-policy",
                                    "x-content-type-options",
                                    "access-control-allow-origin",
                                    "access-control-allow-methods",
                                }
                            }
                            if "html" in content_type or main_document:
                                clean_headers = {
                                    key: value
                                    for key, value in clean_headers.items()
                                    if key.lower() != "content-type"
                                }
                                clean_headers["Content-Type"] = "text/html; charset=utf-8"
                            await route.fulfill(
                                status=fetched.status, headers=clean_headers, body=bytes(body)
                            )
                    except (PolicyError, RenderFailure) as exc:
                        stop(exc.status if isinstance(exc, RenderFailure) else "blocked")
                        with contextlib.suppress(Exception):
                            await route.abort()
                    except Exception:
                        stop("unavailable")
                        with contextlib.suppress(Exception):
                            await route.abort()
                    finally:
                        if counted:
                            inflight -= 1

                await context.route("**/*", route_resource)

                async def navigate():
                    await page.goto(payload["url"], wait_until="domcontentloaded", timeout=12000)
                    ready_until = time.monotonic() + 5
                    while True:
                        snapshot = await page.evaluate(
                            """limit => ({
                                textLength: document.body ? document.body.innerText.trim().length : 0,
                                html: document.documentElement.outerHTML.slice(0, limit + 1)
                            })""",
                            payload["max_bytes"],
                        )
                        if blocked_html(snapshot["html"]):
                            raise RenderFailure("blocked")
                        if snapshot["textLength"] >= 200:
                            break
                        if time.monotonic() >= ready_until:
                            raise RenderFailure("timeout")
                        await page.wait_for_timeout(100)
                    await page.wait_for_timeout(300)
                    validate_public_url(page.url)
                    html = await page.evaluate(
                        """limit => {
                            const html = document.documentElement.outerHTML;
                            if (html.length > limit || new TextEncoder().encode(html).length > limit)
                                return null;
                            return html;
                        }""",
                        payload["max_bytes"],
                    )
                    if html is None:
                        raise RenderFailure("too_large")
                    if (
                        blocked_html(html)
                        or _LOGIN_PATH.search(page.url)
                        or not same_document(page.url, payload["url"])
                    ):
                        raise RenderFailure("blocked")
                    return html.encode("utf-8")

                jobs = [asyncio.create_task(navigate()), asyncio.create_task(blocked.wait())]
                done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                if jobs[1] in done or blocked.is_set():
                    raise RenderFailure(failure[0])
                return jobs[0].result()
        finally:
            for job in jobs:
                job.cancel()
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
            async with asyncio.timeout(2):
                if context is not None:
                    await context.close()
                if browser is not None:
                    await browser.close()


def main():
    if sys.argv[1:] == ["--worker"]:
        try:
            payload = parse_payload(sys.stdin.buffer.read(MAX_REQUEST + 1))
            html = asyncio.run(render_page(payload))
            sys.stdout.buffer.write(html)
        except RenderFailure as exc:
            raise SystemExit(EXIT_CODES.get(exc.status, 23)) from None
        except Exception:
            raise SystemExit(23) from None
        return
    token = os.environ.pop("RENDER_TOKEN", "")
    service = RenderService(token)

    async def serve():
        server = await service.start()
        async with server:
            try:
                await server.serve_forever()
            finally:
                await service.close()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())


if __name__ == "__main__":
    main()
