"""Bounded UTF-8 text downloads from Telegram's authenticated file API."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path

from telegram_bot.telegram_download_worker import MAX_REQUEST_BYTES, remaining

_DOWNLOAD_TIMEOUT_SECONDS = 25
_WORKER = Path(__file__).resolve().with_name("telegram_download_worker.py")


def validate_text_document(document, max_chars: int) -> int:
    name = document.file_name or ""
    size = document.file_size
    limit = max_chars * 4 + 3
    if not name.lower().endswith(".txt"):
        raise ValueError("Upload a UTF-8 .txt file; PDF, HTML and images are not supported here.")
    if not isinstance(size, int) or not 0 < size <= limit:
        raise ValueError(f"The .txt file must be nonempty and at most {limit:,} bytes.")
    return limit


def _run_worker(payload: bytes, limit: int, deadline: float) -> bytes:
    remaining(deadline)
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", str(_WORKER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={},
        cwd="/",
        close_fds=True,
        bufsize=0,
    )
    try:
        data = bytearray()
        pending = memoryview(payload)
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                events = selector.select(remaining(deadline))
                remaining(deadline)
                for key, _ in events:
                    if key.fileobj is process.stdin:
                        written = os.write(key.fd, pending[:4096])
                        pending = pending[written:]
                        if not pending:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        chunk = os.read(key.fd, min(4096, limit + 1 - len(data)))
                        if not chunk:
                            selector.unregister(process.stdout)
                        data.extend(chunk)
                        if len(data) > limit:
                            raise ValueError
        if process.wait(timeout=remaining(deadline)) != 0:
            raise ValueError
        remaining(deadline)
        return bytes(data)
    finally:
        # A timed-out thread cannot free the job slot while DNS/HTTP is hung.
        # This child never spawns descendants; kill and reap it before returning.
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdin.close()
        process.stdout.close()


def download_text_document(bot, document, max_chars: int) -> str:
    limit = validate_text_document(document, max_chars)
    deadline = time.monotonic() + _DOWNLOAD_TIMEOUT_SECONDS
    try:
        payload = json.dumps(
            {"token": bot.token, "file_id": document.file_id, "limit": limit, "deadline": deadline}
        ).encode("ascii")
        if len(payload) > MAX_REQUEST_BYTES:
            raise ValueError
        data = _run_worker(payload, limit, deadline)
        text = data.decode("utf-8-sig")
        if not text.strip() or len(text) > max_chars:
            raise ValueError
        remaining(deadline)
        return text
    except Exception:
        raise ValueError(
            "Could not read that UTF-8 .txt file within the size/time limit. Paste the text instead."
        ) from None
