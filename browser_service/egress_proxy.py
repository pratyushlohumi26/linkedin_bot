"""Small fail-closed HTTP/CONNECT proxy; DNS results are pinned to numeric sockets."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

HEADER_LIMIT = 16384
CONNECTION_LIMIT = 16
CONNECTIONS_PER_MINUTE = 120
BYTES_PER_MINUTE = 64 * 1024 * 1024
CONNECTION_BYTES = 16 * 1024 * 1024
CONNECTION_SECONDS = 30
IDLE_SECONDS = 5
MAX_URL = 4096
_DNS_SLOTS = threading.BoundedSemaphore(4)
_DNS_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="egress-dns")
_PUBLIC_V6 = ipaddress.ip_network("2000::/3")
_DENIED_V4 = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "168.63.129.16/32",
    )
)
_DENIED_V6 = tuple(
    ipaddress.ip_network(net)
    for net in (
        "2001::/23",
        "2001:db8::/32",
        "2002::/16",
        "3fff::/20",
    )
)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


class PolicyError(ValueError):
    pass


def public_ip(value: str):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise PolicyError("blocked") from None
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or (address.version == 4 and any(address in net for net in _DENIED_V4))
        or (
            address.version == 6
            and (
                address not in _PUBLIC_V6
                or any(address in net for net in _DENIED_V6)
                or address.ipv4_mapped is not None
                or address.sixtofour is not None
                or address.teredo is not None
            )
        )
    ):
        raise PolicyError("blocked") from None
    return address


def validate_public_url(url: str) -> tuple[str, int, str]:
    if (
        not isinstance(url, str)
        or not 1 <= len(url) <= MAX_URL
        or any(ord(c) < 33 or ord(c) == 127 for c in url)
        or "\\" in url
    ):
        raise PolicyError("blocked") from None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "%" in parsed.hostname
        ):
            raise PolicyError("blocked") from None
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if port not in {80, 443}:
            raise PolicyError("blocked") from None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (
                len(host) > 253
                or "." not in host
                or all(c.isdigit() or c == "." for c in host)
                or not all(_LABEL.fullmatch(label) for label in host.split("."))
                or host.endswith(
                    (".localhost", ".local", ".internal", ".home", ".test", ".invalid")
                )
            ):
                raise PolicyError("blocked") from None
        else:
            public_ip(str(address))
        return host, port, urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError):
        raise PolicyError("blocked") from None


async def resolve_public_addresses(host: str, port: int) -> list[tuple[int, str]]:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not _DNS_SLOTS.acquire(blocking=False):
            raise PolicyError("blocked") from None
        try:
            future = _DNS_EXECUTOR.submit(
                socket.getaddrinfo,
                host,
                port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
            )
        except BaseException:
            _DNS_SLOTS.release()
            raise
        # Cancellation cannot stop libc DNS; release capacity only when the real work finishes.
        future.add_done_callback(lambda _: _DNS_SLOTS.release())
        async with asyncio.timeout(IDLE_SECONDS):
            results = await asyncio.wrap_future(future)
        if not results or len(results) > 16:
            raise PolicyError("blocked") from None
        addresses = []
        for family, _, _, _, sockaddr in results:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                raise PolicyError("blocked") from None
            # Reject mixed public/private answers, not just the selected address.
            address = public_ip(sockaddr[0])
            item = (family, str(address))
            if item not in addresses:
                addresses.append(item)
        return addresses
    else:
        public_ip(str(address))
        return [(socket.AF_INET if address.version == 4 else socket.AF_INET6, str(address))]


async def connect_public(host: str, port: int):
    if port not in {80, 443}:
        raise PolicyError("blocked") from None
    addresses = await resolve_public_addresses(host, port)
    for family, numeric_ip in addresses:
        public_ip(numeric_ip)
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            # inet_pton-valid addresses make sock_connect bypass DNS entirely.
            socket.inet_pton(family, numeric_ip)
            async with asyncio.timeout(IDLE_SECONDS):
                await asyncio.get_running_loop().sock_connect(sock, (numeric_ip, port))
            return await asyncio.open_connection(sock=sock, limit=HEADER_LIMIT)
        except (OSError, TimeoutError):
            sock.close()
        except BaseException:
            sock.close()
            raise
    raise PolicyError("blocked") from None


def parse_request(raw: bytes) -> tuple[str, str, int, bytes]:
    if len(raw) > HEADER_LIMIT or not raw.endswith(b"\r\n\r\n"):
        raise PolicyError("blocked") from None
    lines = raw[:-4].split(b"\r\n")
    try:
        method, target, version = lines[0].decode("ascii").split(" ")
        if version != "HTTP/1.1" or method not in {"CONNECT", "GET", "HEAD"}:
            raise PolicyError("blocked") from None
        headers = {}
        for line in lines[1:]:
            name, value = line.split(b":", 1)
            name = name.lower()
            if (
                not _HEADER_NAME.fullmatch(name)
                or name in headers
                or any(c < 32 or c == 127 for c in value)
            ):
                raise PolicyError("blocked") from None
            headers[name] = value.strip()
        if (
            b"host" not in headers
            or any(
                h in headers
                for h in (
                    b"transfer-encoding",
                    b"upgrade",
                    b"authorization",
                    b"proxy-authorization",
                    b"expect",
                    b"trailer",
                    b"cookie",
                )
            )
            or headers.get(b"content-length", b"0") != b"0"
            or b"upgrade" in headers.get(b"connection", b"").lower()
        ):
            raise PolicyError("blocked") from None
        if method == "CONNECT":
            host, port, _ = validate_public_url("https://" + target)
            if (
                port != 443
                or any(c in target for c in "/?#")
                or not target.endswith(":443")
                or headers[b"host"].decode("ascii").lower() != target.lower()
            ):
                raise PolicyError("blocked") from None
            return method, host, port, b""
        parsed = urlsplit(target)
        host, port, path = validate_public_url(target)
        if parsed.scheme != "http" or port != 80 or parsed.fragment:
            raise PolicyError("blocked") from None
        supplied_host, supplied_port, supplied_path = validate_public_url(
            "http://" + headers[b"host"].decode("ascii")
        )
        if (supplied_host, supplied_port, supplied_path) != (host, port, "/"):
            raise PolicyError("blocked") from None
        authority = f"[{host}]" if ":" in host else host
        request = f"{method} {path} HTTP/1.1\r\nHost: {authority}\r\n".encode("ascii")
        # Forward a small allowlist, never caller-controlled routing or credentials.
        for name in (b"accept", b"accept-language", b"user-agent", b"range", b"if-none-match"):
            if name in headers:
                request += name + b": " + headers[name] + b"\r\n"
        request += b"Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
        return method, host, port, request
    except (ValueError, UnicodeError):
        raise PolicyError("blocked") from None


class EgressProxy:
    def __init__(self):
        self.active = 0
        self.window = time.monotonic()
        self.connections = 0
        self.transferred = 0
        self.tasks = set()

    def _roll_window(self):
        if time.monotonic() - self.window >= 60:
            self.window = time.monotonic()
            self.connections = self.transferred = 0

    async def _pump(self, reader, writer, budget):
        while True:
            async with asyncio.timeout(IDLE_SECONDS):
                chunk = await reader.read(32768)
                if not chunk:
                    return
                self._roll_window()
                budget[0] += len(chunk)
                self.transferred += len(chunk)
                if budget[0] > CONNECTION_BYTES or self.transferred > BYTES_PER_MINUTE:
                    raise PolicyError("blocked") from None
                writer.write(chunk)
                await writer.drain()

    async def handle(self, reader, writer):
        self._roll_window()
        if (
            self.active >= CONNECTION_LIMIT
            or self.connections >= CONNECTIONS_PER_MINUTE
            or self.transferred >= BYTES_PER_MINUTE
        ):
            writer.close()
            return
        self.active += 1
        self.connections += 1
        self.tasks.add(asyncio.current_task())
        upstream = None
        pumps = []
        connected = False
        try:
            async with asyncio.timeout(CONNECTION_SECONDS):
                async with asyncio.timeout(IDLE_SECONDS):
                    raw = await reader.readuntil(b"\r\n\r\n")
                method, host, port, request = parse_request(raw)
                remote, upstream = await connect_public(host, port)
                budget = [len(raw)]
                if method == "CONNECT":
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                    connected = True
                    pumps = [
                        asyncio.create_task(self._pump(reader, upstream, budget)),
                        asyncio.create_task(self._pump(remote, writer, budget)),
                    ]
                    done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                else:
                    upstream.write(request)
                    await upstream.drain()
                    connected = True
                    await self._pump(remote, writer, budget)
        except (
            PolicyError,
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            if not connected:
                writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
        finally:
            for task in pumps:
                task.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (upstream, writer):
                if stream is not None:
                    stream.close()
            self.active -= 1
            self.tasks.discard(asyncio.current_task())

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


async def main():
    proxy = EgressProxy()
    server = await asyncio.start_server(
        proxy.handle, "0.0.0.0", 8899, limit=HEADER_LIMIT, backlog=32
    )
    async with server:
        try:
            await server.serve_forever()
        finally:
            await proxy.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
