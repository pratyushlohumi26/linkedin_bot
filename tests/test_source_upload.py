"""Real HTTP parsing with fake TCP/TLS, plus real child-process cleanup.

Only network transport and process launch boundaries are replaced: no Telegram
requests, credentials, or production endpoint overrides are used.
"""

import inspect
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path
from threading import BoundedSemaphore
from types import SimpleNamespace

import pytest

from telegram_bot import source_upload
from telegram_bot import telegram_download_worker as worker
from telegram_bot.source_upload import download_text_document, validate_text_document

TOKEN = "12345:test-telegram-token"


def document(name="article.txt", size=40):
    return SimpleNamespace(file_name=name, file_size=size, file_id="file-id")


def bot():
    def forbidden(*args):
        pytest.fail("The unbounded bot.get_file method must not be called")

    return SimpleNamespace(token=TOKEN, get_file=forbidden)


@pytest.mark.parametrize(
    "name,size",
    [
        ("article.pdf", 20),
        ("article.html", 20),
        (None, 20),
        ("article.txt", None),
        ("article.txt", 0),
        ("article.txt", -1),
        ("article.txt", "40"),
        ("article.txt", 4004),
    ],
)
def test_reject_invalid_upload_before_downloading(monkeypatch, name, size):
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **kw: pytest.fail("Invalid upload started a worker")
    )
    with pytest.raises(ValueError):
        validate_text_document(document(name, size), 1000)
    with pytest.raises(ValueError):
        download_text_document(bot(), document(name, size), 1000)


def test_validator_preserves_max_chars_signature_and_byte_limit():
    assert validate_text_document(document("ARTICLE.TXT", 4003), max_chars=1000) == 4003
    assert validate_text_document(document(size=True), 1000) == 4003


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class WireStream(BytesIO):
    def __init__(self, wire, state, phase):
        super().__init__(wire)
        self.state, self.phase = state, phase

    def readline(self, size=-1):
        self.state.clock.now += self.state.delays.get((self.phase, "headers"), 0)
        return super().readline(size)

    def read1(self, size=-1):
        self.state.clock.now += self.state.delays.get((self.phase, "body"), 0)
        return super().read1(min(size, self.state.chunk_size))


class WireSocket:
    def __init__(self, wire, state, phase):
        self.stream = WireStream(wire, state, phase)
        self.sent = bytearray()
        self.closed = False
        self.timeouts = []

    def sendall(self, data):
        self.sent.extend(data)

    def makefile(self, mode):
        assert mode == "rb"
        return self.stream

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def setsockopt(self, *args):
        pass

    def close(self):
        self.closed = True


def response(body, status=200, headers=b"", *, chunked=False, length=True):
    if chunked:
        headers += b"Transfer-Encoding: chunked\r\n"
        body = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
    elif length:
        headers += f"Content-Length: {len(body)}\r\n".encode()
    return f"HTTP/1.1 {status} status\r\n".encode() + headers + b"\r\n" + body


@pytest.fixture
def telegram_http(monkeypatch):
    state = SimpleNamespace(
        data=b"\xef\xbb\xbfThis is the article.\nSecond line.",
        metadata={"ok": True, "result": {"file_path": "documents/file_1.txt", "file_size": 40}},
        metadata_body=None,
        metadata_wire=None,
        file_wire=None,
        metadata_status=200,
        status=200,
        headers=b"",
        metadata_headers=b"",
        chunked=False,
        length=True,
        calls=[],
        tls_calls=[],
        delays={},
        clock=Clock(),
        chunk_size=4096,
    )

    def connect(address, timeout, source_address=None):
        phase = "metadata" if not state.calls else "file"
        state.clock.now += state.delays.get((phase, "connect"), 0)
        assert address == ("api.telegram.org", 443)
        assert 0 < timeout <= 25
        if phase == "metadata":
            body = state.metadata_body
            if body is None:
                body = json.dumps(state.metadata).encode()
            wire = response(body, state.metadata_status, state.metadata_headers)
        else:
            wire = response(
                state.data, state.status, state.headers, chunked=state.chunked, length=state.length
            )
        wire = getattr(state, phase + "_wire") or wire
        sock = WireSocket(wire, state, phase)
        state.calls.append(sock)
        return sock

    def wrap_socket(context, sock, *, server_hostname):
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname
        assert server_hostname == "api.telegram.org"
        state.tls_calls.append(server_hostname)
        return sock

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap_socket)
    monkeypatch.setattr(time, "monotonic", state.clock)
    # Use real worker HTTP in-process for deterministic clock/TCP tests;
    # process supervision and isolated execution are exercised separately.
    monkeypatch.setattr(
        source_upload,
        "_run_worker",
        lambda payload, limit, deadline: worker.download_file(**json.loads(payload)),
    )
    return state


def assert_safe_failure():
    with pytest.raises(ValueError, match="UTF-8") as error:
        download_text_document(bot(), document(), 1000)
    assert TOKEN not in str(error.value)
    assert "http" not in str(error.value)
    assert "api.telegram.org" not in str(error.value)


def test_imports_utf8_bom_and_preserves_lines(telegram_http, monkeypatch):
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NETRC"):
        monkeypatch.setenv(name, "/untrusted-setting")
    assert download_text_document(bot(), document(), 1000) == "This is the article.\nSecond line."
    metadata, download = telegram_http.calls
    assert metadata.sent.startswith(f"POST /bot{TOKEN}/getFile HTTP/1.1\r\n".encode())
    assert metadata.sent.endswith(b"file_id=file-id")
    assert download.sent.startswith(
        f"GET /file/bot{TOKEN}/documents/file_1.txt HTTP/1.1\r\n".encode()
    )
    for sock in telegram_http.calls:
        assert b"Host: api.telegram.org\r\n" in sock.sent
        assert b"Accept-Encoding: identity\r\n" in sock.sent
        assert b"Cookie:" not in sock.sent and b"Authorization:" not in sock.sent
        assert sock.closed and sock.stream.closed
    assert len(telegram_http.tls_calls) == 2


@pytest.mark.parametrize("chunked,length", [(False, True), (True, True), (False, False)])
def test_byte_and_character_limits_with_four_byte_utf8(telegram_http, chunked, length):
    telegram_http.chunked, telegram_http.length = chunked, length
    telegram_http.data = b"\xef\xbb\xbf" + "\U0001f680".encode() * 1000
    assert download_text_document(bot(), document(), 1000) == "\U0001f680" * 1000


@pytest.mark.parametrize(
    "content", [b"\xff\xfe\x00a", b"x" * 4004, b"x" * 1001, b"", b" " * 100, b"\xef\xbb\xbf"]
)
@pytest.mark.parametrize("chunked,length", [(False, True), (True, True), (False, False)])
def test_invalid_or_oversize_actual_file_rejected(telegram_http, content, chunked, length):
    telegram_http.data = content
    telegram_http.chunked, telegram_http.length = chunked, length
    assert_safe_failure()


@pytest.mark.parametrize("phase", ["metadata", "file"])
@pytest.mark.parametrize("status", [301, 302, 307, 401, 429, 500])
def test_non_success_is_not_followed_retried_or_exposed(telegram_http, phase, status):
    if phase == "metadata":
        telegram_http.metadata_status = status
        telegram_http.metadata_headers = b"Location: https://evil.example/secret\r\n"
    else:
        telegram_http.status = status
        telegram_http.headers = b"Location: https://evil.example/secret\r\n"
    assert_safe_failure()
    assert len(telegram_http.calls) == (1 if phase == "metadata" else 2)


@pytest.mark.parametrize(
    "path",
    [
        None,
        12,
        "",
        "../file.txt",
        "/documents/file.txt",
        "documents/../file.txt",
        "documents/./file.txt",
        "documents//file.txt",
        "//evil.example/file.txt",
        "https://evil.example/file.txt",
        "file.txt?token=secret",
        "documents/%2e%2e/file.txt",
        "documents\\file.txt",
        "file.txt#fragment",
        "file.txt\r\nHost: evil.example",
    ],
)
def test_metadata_path_cannot_escape_authenticated_file_prefix(telegram_http, path):
    telegram_http.metadata["result"]["file_path"] = path
    assert_safe_failure()
    assert len(telegram_http.calls) == 1


@pytest.mark.parametrize("size", [4004, -1, "40", [], True])
def test_invalid_metadata_size_rejected_before_file_fetch(telegram_http, size):
    telegram_http.metadata["result"]["file_size"] = size
    assert_safe_failure()
    assert len(telegram_http.calls) == 1


@pytest.mark.parametrize("size", [None, 0, 4003])
def test_missing_or_bounded_metadata_size_still_checks_actual_bytes(telegram_http, size):
    telegram_http.metadata["result"]["file_size"] = size
    assert download_text_document(bot(), document(), 1000)


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON",
        b"\xff",
        b"[]",
        b'{"ok": false}',
        b'{"ok": true, "result": []}',
        b"x" * (16 * 1024 + 1),
    ],
)
def test_invalid_or_oversize_metadata_rejected(telegram_http, body):
    telegram_http.metadata_body = body
    assert_safe_failure()
    assert len(telegram_http.calls) == 1


@pytest.mark.parametrize("phase", ["metadata", "file"])
@pytest.mark.parametrize(
    "headers",
    [
        b"X-Fill: " + b"x" * (32 * 1024) + b"\r\n",
        (b"X-Fill: " + b"x" * 1000 + b"\r\n") * 40,
        b"Content-Encoding: gzip\r\n",
        b"Content-Length: 99999999999\r\n",
        b"Content-Length: invalid\r\n",
        b"Transfer-Encoding: unsupported\r\n",
    ],
)
def test_bounded_headers_and_unsupported_encoding_rejected(telegram_http, phase, headers):
    setattr(telegram_http, "metadata_headers" if phase == "metadata" else "headers", headers)
    assert_safe_failure()


@pytest.mark.parametrize(
    "delays,chunk_size,expected_calls",
    [
        ({("metadata", "connect"): 26}, 4096, 1),
        ({("metadata", "headers"): 9}, 4096, 1),
        ({("metadata", "body"): 26}, 4096, 1),
        ({("file", "connect"): 26}, 4096, 2),
        ({("file", "headers"): 9}, 4096, 2),
        ({("file", "body"): 1}, 1, 2),
        ({("metadata", "body"): 15, ("file", "body"): 11}, 4096, 2),
        ({("metadata", "headers"): 4, ("file", "headers"): 5}, 4096, 2),
    ],
)
def test_metadata_headers_and_body_share_one_absolute_budget(
    telegram_http,
    delays,
    chunk_size,
    expected_calls,
):
    telegram_http.delays = delays
    telegram_http.chunk_size = chunk_size
    assert_safe_failure()
    assert len(telegram_http.calls) == expected_calls
    assert all(sock.closed for sock in telegram_http.calls)


@pytest.fixture
def child_process(monkeypatch):
    real_popen = subprocess.Popen
    state = SimpleNamespace(children=[], code="", kwargs=[])

    def launch(args, **kwargs):
        assert args == [sys.executable, "-I", "-S", str(Path(worker.__file__).resolve())]
        assert TOKEN not in repr(args)
        assert kwargs["env"] == {}
        assert kwargs["close_fds"]
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["stdin"] == kwargs["stdout"] == subprocess.PIPE
        state.kwargs.append(kwargs)
        process = real_popen([*args[:3], "-c", state.code], **kwargs)
        state.children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(source_upload, "_DOWNLOAD_TIMEOUT_SECONDS", 0.4)
    return state


def assert_reaped(process):
    assert process.poll() is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)
    assert process.stdin.closed and process.stdout.closed


@pytest.mark.parametrize("stage", ["stdin", "body", "exit"])
def test_real_subprocess_hang_is_killed_reaped_and_releases_capacity(child_process, stage):
    prelude = "import os, sys, time, signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    if stage != "stdin":
        prelude += "sys.stdin.buffer.read()\n"
    if stage == "body":
        prelude += "sys.stdout.buffer.write(b'partial'); sys.stdout.buffer.flush()\n"
    if stage == "exit":
        prelude += "os.close(1)\n"
    child_process.code = prelude + "time.sleep(60)\n"
    slot = BoundedSemaphore(1)
    started = time.monotonic()
    with slot:
        assert_safe_failure()
    elapsed = time.monotonic() - started
    assert 0.3 <= elapsed < 1.5
    assert slot.acquire(blocking=False)
    slot.release()
    process = child_process.children[0]
    assert process.returncode == -signal.SIGKILL
    assert_reaped(process)


@pytest.mark.parametrize(
    "code",
    [
        "sys.stdout.buffer.write(b'x' * 4004)",
        "sys.stdout.buffer.write(b'valid'); sys.exit(1)",
        "sys.stderr.write('https://api.telegram.org/bot' + request['token']); sys.exit(1)",
        "sys.stdout.buffer.write(b'\\xff')",
    ],
)
def test_worker_output_and_failure_are_bounded_and_sanitized(child_process, code):
    child_process.code = "import sys, json\nrequest = json.load(sys.stdin)\n" + code
    assert_safe_failure()
    assert_reaped(child_process.children[0])


class BlockingWireStream(WireStream):
    def readline(self, size=-1):
        time.sleep(self.state.waits.get((self.phase, "headers"), 0))
        return super().readline(size)

    def read1(self, size=-1):
        time.sleep(self.state.waits.get((self.phase, "body"), 0))
        return super().read1(size)


def worker_script(waits=None):
    metadata = json.dumps({"ok": True, "result": {"file_path": "documents/file.txt"}}).encode()
    wires = [response(metadata), response(b"\xef\xbb\xbfReal worker text\n")]
    return (
        "import json, os, runpy, signal, socket, ssl, time\n"
        "from io import BytesIO\nfrom types import SimpleNamespace\n"
        + inspect.getsource(Clock)
        + "\n"
        + inspect.getsource(WireStream)
        + "\n"
        + inspect.getsource(WireSocket)
        + "\n"
        + inspect.getsource(BlockingWireStream)
        + "\n"
        + f"wires = {wires!r}\n"
        + "assert not any(k in os.environ for k in "
        + "('HTTPS_PROXY', 'NETRC', 'HOME', 'PYTHONPATH', 'SSL_CERT_FILE', 'SSLKEYLOGFILE'))\n"
        + f"state = SimpleNamespace(clock=Clock(), delays={{}}, chunk_size=4096, waits={waits or {}!r})\n"
        + "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        + "def connect(address, timeout, source_address=None):\n"
        + "    assert address == ('api.telegram.org', 443)\n"
        + "    phase = 'metadata' if len(wires) == 2 else 'file'\n"
        + "    time.sleep(state.waits.get((phase, 'connect'), 0))\n"
        + "    sock = WireSocket(b'', state, phase)\n"
        + "    sock.stream = BlockingWireStream(wires.pop(0), state, phase)\n"
        + "    return sock\n"
        + "def wrap(context, sock, *, server_hostname):\n"
        + "    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname\n"
        + "    assert context.keylog_filename is None\n"
        + "    assert server_hostname == 'api.telegram.org'\n"
        + "    return sock\n"
        + "socket.create_connection = connect\nssl.SSLContext.wrap_socket = wrap\n"
        + f"runpy.run_path({str(Path(worker.__file__).resolve())!r}, run_name='__main__')\n"
    )


def test_actual_worker_round_trip_over_fake_trusted_socket(child_process, monkeypatch):
    monkeypatch.setattr(source_upload, "_DOWNLOAD_TIMEOUT_SECONDS", 2)
    child_process.code = worker_script()
    for key in ("HTTPS_PROXY", "NETRC", "HOME", "PYTHONPATH", "SSL_CERT_FILE", "SSLKEYLOGFILE"):
        monkeypatch.setenv(key, "/untrusted-setting")
    assert download_text_document(bot(), document(), 1000) == "Real worker text\n"
    assert child_process.children[0].returncode == 0
    assert_reaped(child_process.children[0])


@pytest.mark.parametrize(
    "waits",
    [
        {("metadata", "connect"): 60},
        {("metadata", "headers"): 60},
        {("metadata", "body"): 60},
        {("file", "connect"): 60},
        {("file", "headers"): 60},
        {("file", "body"): 60},
        {("metadata", "body"): 0.23, ("file", "body"): 0.23},
    ],
)
def test_real_worker_network_hangs_are_killed_with_one_shared_budget(child_process, waits):
    child_process.code = worker_script(waits)
    slot = BoundedSemaphore(1)
    started = time.monotonic()
    with slot:
        assert_safe_failure()
    assert 0.3 <= time.monotonic() - started < 1.5
    assert slot.acquire(blocking=False)
    slot.release()
    assert child_process.children[0].returncode == -signal.SIGKILL
    assert_reaped(child_process.children[0])


def test_worker_continuous_output_is_stopped_at_byte_limit(child_process):
    child_process.code = (
        "import os, sys\nsys.stdin.buffer.read()\n" "while True: os.write(1, b'x' * 4096)\n"
    )
    assert_safe_failure()
    assert_reaped(child_process.children[0])


@pytest.mark.parametrize("token", ["", "no-colon", "123:token/../../path", "123:token?foo", None])
def test_invalid_token_cannot_inject_metadata_path(telegram_http, token):
    with pytest.raises(ValueError, match="UTF-8"):
        download_text_document(SimpleNamespace(token=token), document(), 1000)
    assert not telegram_http.calls


def test_file_id_is_form_encoded_not_a_request_path(telegram_http):
    doc = document()
    doc.file_id = "file&name=foo?value/bar"
    assert download_text_document(bot(), doc, 1000)
    assert telegram_http.calls[0].sent.endswith(b"file_id=file%26name%3Dfoo%3Fvalue%2Fbar")


def test_oversized_request_is_rejected_without_starting_child(child_process):
    doc = document()
    doc.file_id = "x" * 8193
    with pytest.raises(ValueError, match="UTF-8"):
        download_text_document(bot(), doc, 1000)
    assert not child_process.children


@pytest.mark.parametrize("payload", [b"not JSON", b"x" * 8193, b"{}", b"[]", b"null"])
def test_real_worker_invalid_input_fails_silently_before_network(payload):
    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(Path(worker.__file__).resolve())],
        input=payload,
        capture_output=True,
        env={},
        close_fds=True,
        timeout=2,
    )
    assert completed.returncode == 1
    assert completed.stdout == completed.stderr == b""


@pytest.mark.parametrize("phase", ["metadata", "file"])
@pytest.mark.parametrize(
    "wire",
    [
        b"HTTP/1.1 100 Continue\r\n\r\n" * 1400,
        response(b"body")[:-2],
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"1\r\nx\r\n0\r\nX-Trailer: " + b"x" * (32 * 1024) + b"\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"1;extension=" + b"x" * (32 * 1024) + b"\r\nx\r\n0\r\n\r\n",
    ],
)
def test_interim_headers_chunk_framing_and_truncation_are_bounded(telegram_http, phase, wire):
    setattr(telegram_http, phase + "_wire", wire)
    assert_safe_failure()
    assert all(sock.closed and sock.stream.closed for sock in telegram_http.calls)


@pytest.mark.parametrize("chunked", [False, True])
def test_metadata_actual_body_limit_does_not_depend_on_content_length(telegram_http, chunked):
    telegram_http.metadata_wire = response(b"x" * (16 * 1024 + 1), chunked=chunked, length=False)
    assert_safe_failure()
    assert len(telegram_http.calls) == 1


def test_tls_verification_failure_is_not_retried_or_leaked(telegram_http, monkeypatch):
    def untrusted_certificate(*args, **kwargs):
        raise ssl.SSLCertVerificationError("https://api.telegram.org/bot" + TOKEN)

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", untrusted_certificate)
    assert_safe_failure()
    assert len(telegram_http.calls) == 1
    assert telegram_http.calls[0].closed
