"""Trusted, stdlib-only Telegram downloader, supervised by source_upload."""

from __future__ import annotations

import http.client
import json
import math
import re
import ssl
import sys
import time
from functools import partial
from urllib.parse import urlencode

MAX_REQUEST_BYTES = 8192
_METADATA_BYTES = 16 * 1024
_HEADER_BYTES = 32 * 1024
_HOST = "api.telegram.org"


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError
    return seconds


class _BoundedReader:
    def __init__(self, stream, sock, deadline):
        self.stream = stream
        self.sock = sock
        self.deadline = deadline
        self.control_bytes = 0

    def _read(self, method, size):
        self.sock.settimeout(min(5, remaining(self.deadline)))
        data = getattr(self.stream, method)(size)
        remaining(self.deadline)
        return data

    def readline(self, size=-1):
        available = _HEADER_BYTES + 1 - self.control_bytes
        data = self._read("readline", min(size, available) if size >= 0 else available)
        self.control_bytes += len(data)
        if self.control_bytes > _HEADER_BYTES:
            raise ValueError
        return data

    def read(self, size):
        return self._read("read", size)

    def read1(self, size):
        return self._read("read1", size)

    def close(self):
        self.stream.close()


class _BoundedResponse(http.client.HTTPResponse):
    def __init__(self, sock, *args, deadline, **kwargs):
        super().__init__(sock, *args, **kwargs)
        # Includes interim responses, chunk framing and trailers, without
        # changing http.client's process-global parser limits.
        self.fp = _BoundedReader(self.fp, sock, deadline)


def _fetch(path: str, limit: int, deadline: float, body: bytes | None = None) -> bytes:
    connection = http.client.HTTPSConnection(
        _HOST, timeout=min(5, remaining(deadline)), context=ssl.create_default_context()
    )
    connection.response_class = partial(_BoundedResponse, deadline=deadline)
    headers = {"Accept-Encoding": "identity", "Connection": "close"}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        connection.request("POST" if body is not None else "GET", path, body=body, headers=headers)
        remaining(deadline)
        with connection.getresponse() as response:
            remaining(deadline)
            if response.status != 200:
                raise ValueError
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ValueError
            transfer = response.getheader("Transfer-Encoding")
            if transfer is not None and transfer.lower() != "chunked":
                raise ValueError
            lengths = response.headers.get_all("Content-Length", [])
            if lengths:
                if (
                    len(lengths) != 1
                    or transfer is not None
                    or not re.fullmatch(r"[0-9]+", lengths[0])
                    or int(lengths[0]) > limit
                ):
                    raise ValueError
            data = bytearray()
            while True:
                remaining(deadline)
                chunk = response.read1(min(4096, limit + 1 - len(data)))
                remaining(deadline)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError
            if response.length not in (None, 0):
                raise ValueError
            return bytes(data)
    finally:
        connection.close()


def download_file(*, token: str, file_id: str, limit: int, deadline: float) -> bytes:
    if (
        not isinstance(token, str)
        or len(token) > 256
        or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token)
        or not isinstance(file_id, str)
        or not 0 < len(file_id) <= 4096
        or type(limit) is not int
        or limit <= 0
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError
    remaining(deadline)
    metadata = json.loads(
        _fetch(
            f"/bot{token}/getFile",
            _METADATA_BYTES,
            deadline,
            urlencode({"file_id": file_id}).encode("ascii"),
        )
    )
    if not isinstance(metadata, dict) or metadata.get("ok") is not True:
        raise ValueError
    info = metadata.get("result")
    if not isinstance(info, dict):
        raise ValueError
    path = info.get("file_path")
    if (
        not isinstance(path, str)
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError
    size = info.get("file_size")
    if size is not None and (type(size) is not int or not 0 <= size <= limit):
        raise ValueError
    return _fetch(f"/file/bot{token}/{path}", limit, deadline)


def main() -> int:
    try:
        payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(payload) > MAX_REQUEST_BYTES:
            raise ValueError
        data = download_file(**json.loads(payload))
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        # Neither API errors nor tracebacks may expose the token-bearing URL.
        return 1


if __name__ == "__main__":
    sys.exit(main())
