"""Offline parsing in disposable Linux workers; deadlines are absolute monotonic times."""

from __future__ import annotations

import ctypes
import errno
import html as html_module
import json
import math
import os
import platform
import selectors
import signal
import struct
import subprocess
import sys
import threading
import time
from dataclasses import asdict, replace
from html.parser import HTMLParser
from pathlib import Path
from xml.parsers import expat

# Child imports of the application must happen only after installing resource limits.
if __name__ != "__main__":
    from telegram_bot.article_extractor import ExtractedArticle
    from telegram_bot.http_fetcher import FetchError

    class ParseError(FetchError):
        def __init__(self, status: str) -> None:
            if status not in _MESSAGES:
                status = "extraction_failed"
            super().__init__(status, _MESSAGES[status], status == "timeout")


MAX_INPUT_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_FEED_BODY_BYTES = 256 * 1024
MAX_FEED_TOTAL_BYTES = 1024 * 1024
MAX_FEED_ENTRIES = 200
MAX_NODES = 20_000
MAX_DEPTH = 80
MAX_CANDIDATES = 64
MAX_TEXT_CHARS = 200_000
MEMORY_BYTES = 384 * 1024 * 1024
CPU_SECONDS = 4
_METADATA_BYTES = 32 * 1024
_CHUNK = 16 * 1024
_SLOTS = threading.BoundedSemaphore(4)
_MESSAGES = {
    "timeout": "Article parsing reached its time limit. Retry later or supply the text.",
    "too_large": "Source parsing exceeded a safety limit. Supply a smaller text excerpt.",
    "unsupported": "This source format or parser environment is not supported.",
    "extraction_failed": "The source could not be parsed safely. Supply the article text.",
}


class _Rejected(Exception):
    def __init__(self, status="too_large"):
        self.status = status


def _remaining(deadline):
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic time")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ParseError("timeout")
    return remaining


def _worker_env():
    return {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"}


def _reap(proc):
    # SIGKILL, not cooperative cancellation: no timed-out parser survives a released slot.
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait()
    for stream in (proc.stdin, proc.stdout):
        if stream is not None:
            stream.close()


def _exchange(proc, payload, deadline):
    """Bound both pipe directions; communicate() would accumulate unlimited stdout."""
    output = bytearray()
    offset = 0
    try:
        remaining = _remaining(deadline)
        work_deadline = deadline - min(0.1, remaining / 5)
        with selectors.DefaultSelector() as selector:
            os.set_blocking(proc.stdin.fileno(), False)
            os.set_blocking(proc.stdout.fileno(), False)
            if payload:
                selector.register(proc.stdin, selectors.EVENT_WRITE)
            else:
                proc.stdin.close()
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                for key, _ in selector.select(_remaining(work_deadline)):
                    if key.fileobj is proc.stdin:
                        try:
                            count = os.write(proc.stdin.fileno(), payload[offset : offset + _CHUNK])
                            offset += count
                        except BrokenPipeError:
                            offset = len(payload)
                        except BlockingIOError:
                            continue
                        if offset == len(payload):
                            selector.unregister(proc.stdin)
                            proc.stdin.close()
                    else:
                        try:
                            chunk = os.read(
                                proc.stdout.fileno(),
                                min(_CHUNK, MAX_OUTPUT_BYTES + 1 - len(output)),
                            )
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(proc.stdout)
                        else:
                            output.extend(chunk)
                            if len(output) > MAX_OUTPUT_BYTES:
                                raise ParseError("too_large")
                _remaining(work_deadline)
            try:
                proc.wait(timeout=_remaining(work_deadline))
            except subprocess.TimeoutExpired:
                raise ParseError("timeout") from None
        if proc.returncode:
            status = (
                "timeout"
                if proc.returncode in {-signal.SIGXCPU, -signal.SIGKILL}
                else "extraction_failed"
            )
            raise ParseError(status)
        _remaining(deadline)
        return bytes(output)
    finally:
        _reap(proc)


def _invoke(operation, source, deadline, **metadata):
    _remaining(deadline)
    if not isinstance(source, (bytes, str)):
        raise TypeError("source must be bytes or str")
    if len(source) > MAX_INPUT_BYTES:
        raise ParseError("too_large")
    was_text = isinstance(source, str)
    try:
        body = source.encode("utf-8") if was_text else source
    except UnicodeError:
        raise ParseError("unsupported") from None
    if len(body) > MAX_INPUT_BYTES:
        raise ParseError("too_large")
    header = json.dumps(
        {"operation": operation, "text": was_text, **metadata}, ensure_ascii=True
    ).encode("ascii")
    if len(header) > _METADATA_BYTES:
        raise ParseError("too_large")
    payload = struct.pack("!I", len(header)) + header + body
    if not sys.platform.startswith("linux"):
        raise ParseError("unsupported")
    if not _SLOTS.acquire(timeout=_remaining(deadline)):
        raise ParseError("timeout")
    try:
        _remaining(deadline)
        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-B", str(Path(__file__).resolve())],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                env=_worker_env(),
                cwd="/",
            )
            output = _exchange(proc, payload, deadline)
            result = json.loads(output)
        except ParseError:
            raise
        except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
            raise ParseError("extraction_failed") from None
        _remaining(deadline)
        if not isinstance(result, dict) or set(result) not in ({"result"}, {"error"}):
            raise ParseError("extraction_failed")
        if "error" in result:
            raise ParseError(
                result["error"] if isinstance(result["error"], str) else "extraction_failed"
            )
        return result["result"]
    finally:
        _SLOTS.release()


def parse_article(
    html: bytes | str,
    url: str,
    *,
    deadline: float,
    max_text_chars: int = 60000,
    legacy: bool = False,
) -> ExtractedArticle:
    """Retain extractor provenance; reject over-complex input without truncating HTML."""
    if (
        isinstance(max_text_chars, bool)
        or not isinstance(max_text_chars, int)
        or max_text_chars <= 0
    ):
        raise ValueError("max_text_chars must be a positive integer")
    if max_text_chars > MAX_TEXT_CHARS:
        raise ParseError("too_large")
    if not isinstance(url, str):
        raise TypeError("url must be str")
    if len(url) > 16_384:
        raise ParseError("too_large")
    if not isinstance(legacy, bool):
        raise ValueError("legacy must be a boolean")
    result = _invoke(
        "article", html, deadline, url=url, max_text_chars=max_text_chars, legacy=legacy
    )
    try:
        if not isinstance(result, dict) or set(result) != set(
            ExtractedArticle.__dataclass_fields__
        ):
            raise ValueError
        for name, value in result.items():
            if name == "warnings":
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError
            elif name == "needs_render":
                if not isinstance(value, bool):
                    raise ValueError
            elif not isinstance(value, str):
                raise ValueError
        result["warnings"] = tuple(result["warnings"])
        article = ExtractedArticle(**result)
    except (TypeError, ValueError):
        raise ParseError("extraction_failed") from None
    _remaining(deadline)
    return article


def check_challenge(html: bytes | str, headers: dict | None = None, *, deadline: float) -> bool:
    """Detect an interstitial in the worker, including the explicit mitigation header."""
    if headers is not None and not isinstance(headers, dict):
        raise TypeError("headers must be dict or None")
    # Do not forward cookies, authorization, or unrelated response headers to the child.
    mitigated = False
    if headers:
        if len(headers) > 256:
            raise ParseError("too_large")
        mitigated = any(
            isinstance(key, str)
            and key.lower() == "cf-mitigated"
            and isinstance(value, str)
            and value.strip().lower() == "challenge"
            for key, value in headers.items()
        )
    result = _invoke("challenge", html, deadline, mitigated=mitigated)
    if not isinstance(result, bool):
        raise ParseError("extraction_failed")
    return result


def feed_entries(body: bytes, *, deadline: float) -> list[dict[str, str]]:
    """Return bounded RSS/Atom sources, not extracted or completeness-approved articles."""
    if not isinstance(body, bytes):
        raise TypeError("body must be bytes")
    result = _invoke("feed", body, deadline)
    if (
        not isinstance(result, list)
        or len(result) > MAX_FEED_ENTRIES
        or any(
            not isinstance(entry, dict)
            or set(entry) != {"link", "title", "body", "body_kind"}
            or any(not isinstance(value, str) for value in entry.values())
            or entry["body_kind"] not in {"content", "description"}
            for entry in result
        )
    ):
        raise ParseError("extraction_failed")
    return result


def _sandbox():
    import resource

    if not sys.platform.startswith("linux"):
        raise _Rejected("unsupported")
    for kind, value in (
        (resource.RLIMIT_AS, MEMORY_BYTES),
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_FSIZE, 0),
        (resource.RLIMIT_NOFILE, 32),
    ):
        _, hard = resource.getrlimit(kind)
        limit = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(kind, (limit, limit))
    _, hard_cpu = resource.getrlimit(resource.RLIMIT_CPU)
    hard_cpu = (
        CPU_SECONDS + 1 if hard_cpu == resource.RLIM_INFINITY else min(CPU_SECONDS + 1, hard_cpu)
    )
    resource.setrlimit(resource.RLIMIT_CPU, (min(CPU_SECONDS, hard_cpu), hard_cpu))
    # Deny sockets and process creation in the kernel, including native-library calls.
    arch = platform.machine().lower()
    if arch in {"x86_64", "amd64"}:
        audit_arch = 0xC000003E
        denied = (41, 42, 43, 49, 50, 53, 56, 57, 58, 59, 101, 288, 322, 435)
    elif arch in {"aarch64", "arm64"}:
        audit_arch = 0xC00000B7
        denied = (117, 198, 199, 200, 201, 202, 203, 220, 221, 242, 281, 435)
    else:
        raise _Rejected("unsupported")

    class Filter(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_ushort),
            ("jt", ctypes.c_ubyte),
            ("jf", ctypes.c_ubyte),
            ("k", ctypes.c_uint),
        ]

    class Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]

    instructions = [
        (0x20, 0, 0, 4),
        (0x15, 1, 0, audit_arch),
        (0x06, 0, 0, 0x80000000),
        (0x20, 0, 0, 0),
        # x32 syscalls must not bypass the x86-64 deny list.
        (0x45, 0, 1, 0x40000000),
        (0x06, 0, 0, 0x00050000 | errno.EPERM),
    ]
    for number in denied:
        instructions.extend(((0x15, 0, 1, number), (0x06, 0, 0, 0x00050000 | errno.EPERM)))
    instructions.append((0x06, 0, 0, 0x7FFF0000))
    filters = (Filter * len(instructions))(*(Filter(*item) for item in instructions))
    program = Program(len(filters), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise _Rejected("unsupported")


class _HTMLBudget(HTMLParser):
    _VOID = frozenset(
        "area base br col embed hr img input link meta param source track wbr".split()
    )

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.nodes = 0
        self.candidates = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if tag in {"main", "article"} or any(
            key == "role" and value == "main" for key, value in attrs
        ):
            self.candidates += 1
        if self.nodes > MAX_NODES or self.candidates > MAX_CANDIDATES or len(attrs) > 128:
            raise _Rejected()
        if tag not in self._VOID:
            self.stack.append(tag)
            if len(self.stack) > MAX_DEPTH:
                raise _Rejected()

    def handle_endtag(self, tag):
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


def _check_html(source):
    # UTF-16/32 bytes could hide tags from the preflight while BS4 detects them later.
    if isinstance(source, bytes):
        if b"\0" in source:
            raise _Rejected("unsupported")
        text = source.decode("latin-1")
    else:
        text = source
    parser = _HTMLBudget()
    for offset in range(0, len(text), _CHUNK):
        parser.feed(text[offset : offset + _CHUNK])
    parser.close()


class _Parts:
    def __init__(self, limit):
        self.limit = limit
        self.size = 0
        self.parts = []

    def add(self, text):
        self.size += len(text.encode("utf-8"))
        if self.size > self.limit:
            raise _Rejected()
        self.parts.append(text)

    def value(self):
        return "".join(self.parts)


def _local(name):
    return name.rsplit("}", 1)[-1]


class _FeedParser:
    def __init__(self):
        self.stack = []
        self.nodes = 0
        self.entries_seen = 0
        self.entry = None
        self.entry_depth = 0
        self.field = None
        self.fields = {}
        self.output = []
        self.total = 0
        self.atom = False

    def start(self, name, attrs):
        tag = _local(name)
        self.stack.append(tag)
        self.nodes += 1
        if len(self.stack) > MAX_DEPTH or self.nodes > MAX_NODES or len(attrs) > 128:
            raise _Rejected()
        if len(self.stack) == 1:
            if tag not in {"rss", "RDF", "feed"}:
                raise _Rejected("unsupported")
            self.atom = tag == "feed"
        if tag in {"item", "entry"}:
            if self.entry is not None:
                raise _Rejected("unsupported")
            self.entries_seen += 1
            if self.entries_seen > MAX_FEED_ENTRIES:
                raise _Rejected()
            self.entry = {"link": "", "title": ""}
            self.entry_depth = len(self.stack)
            self.fields = {}
        elif self.entry is not None and len(self.stack) == self.entry_depth + 1:
            if tag in {"title", "link", "encoded", "content", "description", "summary"}:
                if self.atom and tag == "link":
                    if attrs.get("rel", "alternate") == "alternate" and not self.entry["link"]:
                        link = attrs.get("href", "")
                        if len(link.encode("utf-8")) > 16_384:
                            raise _Rejected()
                        self.entry["link"] = link
                    return
                if tag in self.fields:
                    raise _Rejected("unsupported")
                kind = attrs.get("type", "text") if self.atom else "html"
                if (
                    tag in {"content", "summary"}
                    and self.atom
                    and ("src" in attrs or kind not in {"text", "html", "xhtml", "text/html"})
                ):
                    return
                limit = 16_384 if tag in {"title", "link"} else MAX_FEED_BODY_BYTES
                parts = _Parts(limit)
                self.fields[tag] = parts
                self.field = (tag, kind, parts)
        elif self.field:
            parts = self.field[2]
            parts.add("<" + tag)
            for key, value in attrs.items():
                parts.add(" " + _local(key) + '="' + html_module.escape(value, quote=True) + '"')
            parts.add(">")

    def data(self, value):
        if self.field:
            tag, kind, parts = self.field
            nested = len(self.stack) > self.entry_depth + 1
            if nested or (self.atom and kind == "text" and tag not in {"title", "link"}):
                value = html_module.escape(value, quote=False)
            parts.add(value)

    def end(self, name):
        tag = _local(name)
        if self.field and len(self.stack) > self.entry_depth + 1:
            self.field[2].add("</" + tag + ">")
        elif self.field and len(self.stack) == self.entry_depth + 1:
            self.field = None
        if self.entry is not None and len(self.stack) == self.entry_depth:
            for key in ("title", "link"):
                if key in self.fields and not self.entry[key]:
                    self.entry[key] = self.fields[key].value()
            body_key = next(
                (
                    key
                    for key in ("encoded", "content", "description", "summary")
                    if key in self.fields
                ),
                None,
            )
            if body_key:
                body = self.fields[body_key].value()
                entry = {
                    **self.entry,
                    "body": body,
                    "body_kind": "content" if body_key in {"encoded", "content"} else "description",
                }
                self.total += sum(len(value.encode("utf-8")) for value in entry.values())
                if self.total > MAX_FEED_TOTAL_BYTES:
                    raise _Rejected()
                self.output.append(entry)
            self.entry = None
        self.stack.pop()


def _parse_feed(body):
    target = _FeedParser()
    parser = expat.ParserCreate(namespace_separator="}")
    parser.StartElementHandler = target.start
    parser.EndElementHandler = target.end
    parser.CharacterDataHandler = target.data

    def reject(*args):
        raise _Rejected("unsupported")

    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    for offset in range(0, len(body), _CHUNK):
        parser.Parse(body[offset : offset + _CHUNK], False)
    parser.Parse(b"", True)
    return target.output


def _dispatch(metadata, body):
    operation = metadata["operation"]
    if operation == "feed":
        return _parse_feed(body)
    source = body.decode("utf-8") if metadata["text"] else body
    if operation == "challenge" and metadata["mitigated"]:
        return True
    _check_html(source)
    from telegram_bot.article_extractor import extract_article, is_access_challenge

    if operation == "challenge":
        return is_access_challenge(source)
    if operation == "article":
        article = extract_article(
            source, metadata["url"], max_text_chars=metadata["max_text_chars"]
        )
        if metadata.get("legacy") and article.text:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(source, "html.parser")
            for node in soup(["script", "style", "nav", "footer"]):
                node.decompose()
            article = replace(
                article,
                text=soup.get_text("\n", strip=True)[: metadata["max_text_chars"]],
                method="legacy",
                completeness="partial",
                warnings=(
                    *article.warnings,
                    "Legacy extraction may contain unrelated page text; inspect before continuing.",
                ),
            )
        return asdict(article)
    raise _Rejected("unsupported")


def _encode_result(result):
    output = bytearray()
    for piece in json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).iterencode(result):
        encoded = piece.encode("utf-8")
        if len(output) + len(encoded) > MAX_OUTPUT_BYTES:
            raise _Rejected()
        output.extend(encoded)
    return output


def _main():
    try:
        _sandbox()
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        stream = sys.stdin.buffer
        size_bytes = stream.read(4)
        if len(size_bytes) != 4:
            raise _Rejected("extraction_failed")
        size = struct.unpack("!I", size_bytes)[0]
        if size > _METADATA_BYTES:
            raise _Rejected()
        metadata = json.loads(stream.read(size))
        body = stream.read(MAX_INPUT_BYTES + 1)
        if len(body) > MAX_INPUT_BYTES:
            raise _Rejected()
        output = _encode_result({"result": _dispatch(metadata, body)})
    except _Rejected as exc:
        output = json.dumps({"error": exc.status}).encode("ascii")
    except (MemoryError, RecursionError):
        output = b'{"error":"too_large"}'
    except Exception:
        output = b'{"error":"extraction_failed"}'
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    _main()
