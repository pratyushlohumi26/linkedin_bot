"""Public HTTP(S) fetching with numeric-IP pinning and bounded resource use."""

from __future__ import annotations

import http.client
import ipaddress
import math
import queue
import re
import socket
import ssl
import threading
import time
import unicodedata
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

if TYPE_CHECKING:
    from telegram_bot.config import ScraperConfig

_REDIRECTS = {301, 302, 303, 307, 308}
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_RESOLVER_SLOTS = threading.BoundedSemaphore(8)
_RESOLVE_TIMEOUT = 5.0
_CHUNK_BYTES = 16 * 1024
_MAX_DOMAINS = 1024
_THROTTLE_LOCK = threading.Lock()
_DOMAIN_READY: dict[str, float] = {}
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_ESCAPED_CONTROL = re.compile(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", re.IGNORECASE)


@dataclass(frozen=True)
class FetchResponse:
    requested_url: str = field(repr=False)
    url: str = field(repr=False)
    status_code: int
    headers: dict[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    elapsed_seconds: float
    attempts: int


class FetchError(Exception):
    def __init__(self, status: str, message: str, retryable: bool = False) -> None:
        self.status = status
        self.message = message
        self.retryable = retryable
        super().__init__(message)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchError("timeout", "The page fetch time limit was reached.", True) from None
    return remaining


def _public_ip(address: str) -> str:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        raise FetchError("blocked", "The destination must use a public IP address.") from None
    checked = parsed.ipv4_mapped if isinstance(parsed, ipaddress.IPv6Address) else None
    checked = checked or parsed
    if (
        not checked.is_global
        or str(checked) == "168.63.129.16"
        or checked.is_reserved
        or checked.is_multicast
        or checked.is_unspecified
        or checked.is_loopback
        or checked.is_link_local
        or (
            isinstance(parsed, ipaddress.IPv6Address)
            and (parsed.scope_id or parsed.is_site_local or parsed.sixtofour or parsed.teredo)
        )
    ):
        raise FetchError("blocked", "The destination must use only public IP addresses.")
    return str(parsed)


def _normalize_host(host: str) -> str:
    if (
        not isinstance(host, str)
        or not host
        or "%" in host
        or any(c.isspace() or unicodedata.category(c).startswith("C") for c in host)
    ):
        raise FetchError("invalid_url", "The URL hostname is invalid.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return _public_ip(host)
    try:
        normalized = host.encode("idna").decode("ascii").lower().removesuffix(".")
    except UnicodeError:
        raise FetchError("invalid_url", "The URL hostname is invalid.") from None
    labels = normalized.split(".")
    if len(normalized) > 253 or any(not _LABEL.fullmatch(label) for label in labels):
        raise FetchError("invalid_url", "The URL hostname is invalid.")
    if (
        len(labels) < 2
        or labels[-1].isdigit()
        or all(re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", label) for label in labels)
        or normalized.endswith(
            (".localhost", ".local", ".internal", ".home.arpa", ".invalid", ".test")
        )
    ):
        raise FetchError("blocked", "Internal or non-public hostnames are not allowed.")
    return normalized


def validate_public_url(url: str) -> str:
    """Normalize a public URL syntactically; resolve_public_addresses checks its DNS."""
    if (
        not isinstance(url, str)
        or not url
        or len(url) > 16_384
        or "\\" in url
        or any(
            character.isspace() or unicodedata.category(character).startswith("C")
            for character in url
        )
        or _ESCAPED_CONTROL.search(url)
    ):
        raise FetchError("invalid_url", "Provide a valid public HTTP or HTTPS URL.")
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise FetchError("invalid_url", "Only HTTP and HTTPS URLs are supported.")
        if parsed.username is not None or parsed.password is not None:
            raise FetchError("invalid_url", "URLs containing credentials are not allowed.")
        if not parsed.hostname or parsed.netloc.endswith(":"):
            raise FetchError("invalid_url", "The URL hostname or port is invalid.")
        port = parsed.port or (443 if scheme == "https" else 80)
        if parsed.port == 0 or port not in {80, 443}:
            raise FetchError("blocked", "Only HTTP ports 80 and 443 are allowed.")
        host = _normalize_host(parsed.hostname)
    except ValueError:
        raise FetchError("invalid_url", "Provide a valid public HTTP or HTTPS URL.") from None
    authority = f"[{host}]" if ":" in host else host
    if port != (443 if scheme == "https" else 80):
        authority += f":{port}"
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
    return urlunsplit((scheme, authority, path, query, ""))


def _resolve(host: str, port: int, deadline: float) -> list[str]:
    if port not in {80, 443}:
        raise FetchError("blocked", "Only HTTP ports 80 and 443 are allowed.")
    host = _normalize_host(host)
    _remaining(deadline)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [host]
    if not _RESOLVER_SLOTS.acquire(timeout=_remaining(deadline)):
        raise FetchError("timeout", "Name resolution time limit was reached.", True)
    result: queue.Queue[list[str] | FetchError] = queue.Queue(maxsize=1)

    def lookup() -> None:
        try:
            records = socket.getaddrinfo(
                host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP
            )
            if not records or len(records) > 64:
                raise FetchError("fetch_failed", "The public hostname could not be resolved.")
            addresses = list(dict.fromkeys(_public_ip(record[4][0]) for record in records))
            result.put_nowait(addresses)
        except FetchError as exc:
            result.put_nowait(exc)
        except OSError as exc:
            result.put_nowait(
                FetchError(
                    "fetch_failed",
                    "The public hostname could not be resolved.",
                    isinstance(exc, socket.gaierror) and exc.errno == socket.EAI_AGAIN,
                )
            )
        finally:
            _RESOLVER_SLOTS.release()

    # Native getaddrinfo cannot be cancelled; timed-out workers are daemonized and capped.
    worker = threading.Thread(target=lookup, name="public-http-dns", daemon=True)
    try:
        worker.start()
    except RuntimeError:
        _RESOLVER_SLOTS.release()
        raise FetchError(
            "fetch_failed", "Name resolution is temporarily unavailable.", True
        ) from None
    try:
        answer = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise FetchError("timeout", "Name resolution time limit was reached.", True) from None
    _remaining(deadline)
    if isinstance(answer, FetchError):
        raise answer from None
    return answer


def resolve_public_addresses(host: str, port: int) -> list[str]:
    """Resolve within five seconds, rejecting the entire answer if any IP is not public.

    Callers must connect to a returned numeric address, never resolve the hostname again.
    """
    return _resolve(host, port, time.monotonic() + _RESOLVE_TIMEOUT)


def _throttle(host: str, interval: float, deadline: float) -> None:
    if interval <= 0:
        _remaining(deadline)
        return
    while True:
        _remaining(deadline)
        with _THROTTLE_LOCK:
            now = time.monotonic()
            for expired in [key for key, ready in _DOMAIN_READY.items() if ready <= now]:
                del _DOMAIN_READY[expired]
            ready = _DOMAIN_READY.get(host, now)
            if ready <= now:
                if len(_DOMAIN_READY) >= _MAX_DOMAINS:
                    raise FetchError(
                        "rate_limited", "The page fetcher is busy. Please try again later.", True
                    )
                _DOMAIN_READY[host] = now + interval
                return
        delay = ready - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, _remaining(deadline)))


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(
        self, host: str, port: int, address: str, *, tls: bool, timeout: float, deadline: float
    ) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address
        self._tls = tls
        self._deadline = deadline
        self._socket_lock = threading.Lock()
        self._active_socket: socket.socket | None = None
        self._watchdog: threading.Timer | None = None
        self.set_debuglevel(0)

    def _abort(self) -> None:
        with self._socket_lock:
            if self._active_socket is not None:
                try:
                    self._active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def connect(self) -> None:
        family = socket.AF_INET6 if ":" in self._address else socket.AF_INET
        raw = socket.socket(family, socket.SOCK_STREAM)
        self.sock = self._active_socket = raw
        raw.settimeout(min(self.timeout, _remaining(self._deadline)))
        self._watchdog = threading.Timer(_remaining(self._deadline), self._abort)
        self._watchdog.daemon = True
        self._watchdog.start()
        destination = (
            (self._address, self.port, 0, 0)
            if family == socket.AF_INET6
            else (self._address, self.port)
        )
        # socket.connect receives a numeric literal, bypassing getaddrinfo entirely.
        raw.connect(destination)
        if self._tls:
            context = ssl.create_default_context()
            with self._socket_lock:
                _remaining(self._deadline)
                self.sock = self._active_socket = context.wrap_socket(
                    raw, server_hostname=self.host, do_handshake_on_connect=False
                )
            self.sock.settimeout(min(self.timeout, _remaining(self._deadline)))
            self.sock.do_handshake()
        _remaining(self._deadline)

    def read_timeout(self, timeout: float) -> None:
        if self._active_socket is not None:
            self._active_socket.settimeout(min(timeout, _remaining(self._deadline)))

    def finish(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        self.close()
        if self._active_socket is not None:
            self._active_socket.close()


class _Decoder:
    def __init__(self, encoding: str) -> None:
        self.encoding = encoding
        self.decoder = None
        self.prefix = b""

    def append(self, data: bytes, output: bytearray, limit: int, deadline: float) -> None:
        if self.encoding in {"", "identity"}:
            output.extend(data)
            if len(output) > limit:
                raise FetchError("too_large", "The page exceeds the response size limit.")
            return
        if self.decoder is None:
            self.prefix += data
            if len(self.prefix) < 2:
                return
            data, self.prefix = self.prefix, b""
            wbits = zlib.MAX_WBITS | 16
            if self.encoding == "deflate":
                header = data[0] * 256 + data[1]
                wrapped = data[0] & 15 == 8 and data[0] >> 4 <= 7 and header % 31 == 0
                wbits = zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS
            self.decoder = zlib.decompressobj(wbits)
        while data:
            _remaining(deadline)
            if self.decoder.eof:
                if self.encoding != "gzip":
                    raise FetchError("fetch_failed", "The page compression is invalid.")
                self.decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)
            output.extend(self.decoder.decompress(data, min(_CHUNK_BYTES, limit + 1 - len(output))))
            if len(output) > limit:
                raise FetchError("too_large", "The page exceeds the response size limit.")
            data = self.decoder.unused_data if self.decoder.eof else self.decoder.unconsumed_tail

    def finish(self) -> None:
        if self.encoding not in {"", "identity"} and (self.decoder is None or not self.decoder.eof):
            raise FetchError("fetch_failed", "The page compression is incomplete.")


def _read_body(
    response: http.client.HTTPResponse,
    connection: _PinnedConnection,
    config: ScraperConfig,
    deadline: float,
) -> bytes:
    if response.status in {204, 304} or 100 <= response.status < 200:
        return b""
    encoding = response.getheader("Content-Encoding", "").strip().lower()
    if encoding not in {"", "identity", "gzip", "deflate"}:
        raise FetchError("unsupported", "The page uses an unsupported content encoding.")
    limit = config.max_response_bytes
    wire_limit = limit if encoding in {"", "identity"} else max(limit * 2, 64 * 1024)
    content_length = response.getheader("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise FetchError("fetch_failed", "The page returned invalid HTTP framing.") from None
        if declared_length < 0:
            raise FetchError("fetch_failed", "The page returned invalid HTTP framing.")
        if declared_length > wire_limit:
            raise FetchError("too_large", "The page exceeds the response size limit.")
    else:
        declared_length = None
    transfer_encoding = response.getheader("Transfer-Encoding", "").lower().strip()
    if transfer_encoding and (transfer_encoding != "chunked" or content_length is not None):
        raise FetchError("fetch_failed", "The page returned invalid HTTP framing.")
    output = bytearray()
    received = 0
    decoder = _Decoder(encoding)
    while not response.isclosed():
        connection.read_timeout(config.timeout_seconds)
        data = response.read1(min(_CHUNK_BYTES, wire_limit + 1 - received))
        _remaining(deadline)
        if not data:
            break
        received += len(data)
        if received > wire_limit:
            raise FetchError("too_large", "The page exceeds the response size limit.")
        decoder.append(data, output, limit, deadline)
    if (
        declared_length is not None
        and response.status not in {204, 304}
        and received != declared_length
    ):
        raise FetchError("fetch_failed", "The page response was incomplete.")
    decoder.finish()
    return bytes(output)


def _request(
    url: str, address: str, config: ScraperConfig, deadline: float
) -> tuple[int, dict[str, str], bytes]:
    parsed = urlsplit(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection = _PinnedConnection(
        host,
        port,
        address,
        tls=parsed.scheme == "https",
        timeout=min(config.connect_timeout_seconds, _remaining(deadline)),
        deadline=deadline,
    )
    response = None
    connecting = True
    try:
        connection.connect()
        connecting = False
        connection.read_timeout(config.timeout_seconds)
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        connection.request(
            "GET",
            target,
            headers={
                "Host": parsed.netloc,
                "Accept": "text/html, application/xhtml+xml, text/plain;q=0.9, */*;q=0.1",
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": "PublicArticleFetcher/1.0",
                "Connection": "close",
            },
        )
        connection.read_timeout(config.timeout_seconds)
        response = connection.getresponse()
        _remaining(deadline)
        headers = {}
        for key, value in response.getheaders():
            key = key.lower()
            if key in headers:
                if key in {"location", "content-type", "cf-mitigated"}:
                    raise FetchError("fetch_failed", "The page returned ambiguous HTTP headers.")
                headers[key] += ", " + value
            else:
                headers[key] = value
        # Redirect bodies are not needed and must not consume the remaining fetch budget.
        body = (
            b""
            if response.status in _REDIRECTS and "location" in headers
            else _read_body(response, connection, config, deadline)
        )
        return response.status, headers, body
    except TimeoutError:
        raise FetchError("timeout", "The page request timed out.", True) from None
    except ssl.SSLError:
        _remaining(deadline)
        raise FetchError(
            "fetch_failed", "A verified TLS connection could not be established."
        ) from None
    except (OSError, http.client.HTTPException, zlib.error):
        _remaining(deadline)
        raise FetchError(
            "fetch_failed", "The page request could not be completed.", connecting
        ) from None
    finally:
        if response is not None:
            response.close()
        connection.finish()


def _retry_delay(headers: dict[str, str], attempt: int) -> float:
    fallback = min(0.25 * (2 ** min(attempt - 1, 6)), 5.0)
    value = headers.get("retry-after", "").strip()
    if value.isascii() and value.isdigit():
        return float(value) if len(value) < 10 else math.inf
    if value:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return max(0.0, (when - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return fallback


class SafeFetcher:
    def __init__(self, config: ScraperConfig) -> None:
        self.config = config

    def fetch(
        self,
        url: str,
        *,
        deadline: float | None = None,
        before_request: Callable[[str, float], float | None] | None = None,
    ) -> FetchResponse:
        """Fetch with a shared monotonic deadline; attempts count retries, not redirects."""
        started = time.monotonic()
        expires = started + self.config.total_timeout_seconds
        if deadline is not None:
            if not math.isfinite(deadline):
                raise FetchError("invalid_url", "The fetch deadline is invalid.")
            expires = min(expires, deadline)
        current = validate_public_url(url)
        attempt = 1
        redirects = 0
        while True:
            _remaining(expires)
            parsed = urlsplit(current)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            policy_interval = before_request(current, expires) if before_request else 0
            try:
                addresses = _resolve(
                    parsed.hostname,
                    port,
                    min(expires, time.monotonic() + self.config.connect_timeout_seconds),
                )
                _throttle(
                    parsed.hostname,
                    max(self.config.min_interval_seconds, policy_interval or 0),
                    expires,
                )
                address = addresses[(attempt - 1) % len(addresses)]
                status, headers, body = _request(current, address, self.config, expires)
            except FetchError as exc:
                if not exc.retryable or attempt >= self.config.max_attempts:
                    raise
                delay = _retry_delay({}, attempt)
                if delay >= _remaining(expires):
                    raise
                time.sleep(delay)
                attempt += 1
                continue
            _remaining(expires)
            if status in _REDIRECTS and "location" in headers:
                if redirects >= self.config.max_redirects:
                    raise FetchError("fetch_failed", "The page exceeded the redirect limit.")
                location = headers["location"]
                if (
                    "\\" in location
                    or any(unicodedata.category(c).startswith("C") for c in location)
                    or location != location.strip()
                ):
                    raise FetchError("invalid_url", "The page returned an invalid redirect.")
                try:
                    target = urljoin(current, location)
                except ValueError:
                    raise FetchError(
                        "invalid_url", "The page returned an invalid redirect."
                    ) from None
                current = validate_public_url(target)
                redirects += 1
                continue
            if status in _RETRY_STATUSES and attempt < self.config.max_attempts:
                delay = _retry_delay(headers, attempt)
                if delay < _remaining(expires):
                    time.sleep(delay)
                    attempt += 1
                    continue
            return FetchResponse(
                url, current, status, headers, body, time.monotonic() - started, attempt
            )
