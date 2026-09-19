"""Real, offline subprocess tests; no parser or process mocks."""

import html
import json
import os
import resource
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from telegram_bot import parse_worker as worker
from telegram_bot.article_extractor import extract_article, is_access_challenge
from telegram_bot.http_fetcher import FetchError

URL = "https://example.com/story?token=do-not-log"
TEXT = (
    "A small research group tested a new cooling system in its workshop. "
    "The first experiment reduced heat without increasing the machine's power demand."
)
ARTICLE = f"<article><h1>Workshop report</h1><p>{TEXT}</p></article>"


def deadline(seconds=8):
    return time.monotonic() + seconds


def rss(entries):
    return (
        '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>'
        + entries
        + "</channel></rss>"
    ).encode()


@pytest.mark.parametrize("source", [ARTICLE, ARTICLE.encode(), "", "<main>Menu</main>"])
def test_article_preserves_existing_extractor(source):
    assert worker.parse_article(source, URL, deadline=deadline()) == extract_article(source, URL)


@pytest.mark.parametrize(
    "source",
    [
        ARTICLE.replace("</article>", "<p>Subscribe to continue reading.</p></article>"),
        '<meta name="description" content="' + TEXT + '">',
        '<script type="application/ld+json">'
        + json.dumps({"@type": "Article", "articleBody": TEXT})
        + "</script>",
        '<link rel="canonical" href="https://evil.example/story">' + ARTICLE,
        '<div id="root"></div><script src="/app.js"></script>',
    ],
)
def test_provenance_is_not_promoted(source):
    assert worker.parse_article(source, URL, deadline=deadline()) == extract_article(source, URL)


def test_text_limit_retains_partial_warning():
    result = worker.parse_article(ARTICLE, URL, deadline=deadline(), max_text_chars=40)
    assert len(result.text) <= 40
    assert result.completeness == "partial"
    assert "text_truncated" in result.warnings


@pytest.mark.parametrize(
    "source,headers",
    [
        (ARTICLE, None),
        ('<title>Just a moment...</title><div id="challenge-running"></div>', None),
        ("", {"CF-Mitigated": "challenge"}),
        ("<h1>Sign in</h1><input type=password>", {}),
    ],
)
def test_challenge_uses_real_existing_detector(source, headers):
    assert worker.check_challenge(source, headers, deadline=deadline()) is is_access_challenge(
        source, headers
    )


def test_rss_content_wins_over_description_without_rewriting_source():
    body = "<p>A &amp; B. Original <strong>source</strong>.</p>"
    entries = worker.feed_entries(
        rss(
            f"<item><link>{html.escape(URL)}</link><title>A &amp; B</title>"
            f"<description>Teaser</description><content:encoded><![CDATA[{body}]]>"
            "</content:encoded></item>"
        ),
        deadline=deadline(),
    )
    assert entries == [{"link": URL, "title": "A & B", "body": body, "body_kind": "content"}]


def test_rss_description_is_not_full_content():
    assert worker.feed_entries(
        rss(
            "<item><link>https://example.com/a</link><description>Only teaser</description></item>"
        ),
        deadline=deadline(),
    ) == [
        {
            "link": "https://example.com/a",
            "title": "",
            "body": "Only teaser",
            "body_kind": "description",
        }
    ]


def test_atom_alternate_content_and_summary():
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom">
    <entry><link rel="self" href="https://example.com/api/1"/>
    <link rel="alternate" href="https://example.com/a"/><title>Source</title>
    <summary>teaser</summary><content type="html">&lt;p&gt;Original &amp;amp; text&lt;/p&gt;</content></entry>
    <entry><link href="https://example.com/b"/><summary type="text">Not a &lt;script&gt;</summary></entry>
    </feed>"""
    entries = worker.feed_entries(body, deadline=deadline())
    assert entries[0] == {
        "link": "https://example.com/a",
        "title": "Source",
        "body": "<p>Original &amp; text</p>",
        "body_kind": "content",
    }
    assert entries[1]["body"] == "Not a &lt;script&gt;"
    assert entries[1]["body_kind"] == "description"


def test_atom_xhtml_preserves_markup_text_and_tails():
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <link href="https://example.com/a"/><content type="xhtml">
    <div xmlns="http://www.w3.org/1999/xhtml"><p>Before <b>bold</b> after.</p></div>
    </content></entry></feed>"""
    result = worker.feed_entries(body, deadline=deadline())[0]
    assert "<p>Before <b>bold</b> after.</p>" in result["body"]
    assert "ns0" not in result["body"]


@pytest.mark.parametrize(
    "body",
    [
        b"<rss><channel><item></channel></rss>",
        b'<!DOCTYPE rss [<!ENTITY x "SECRET">]><rss><channel><item>&x;</item></channel></rss>',
        b'<!DOCTYPE rss SYSTEM "http://127.0.0.1/private"><rss/>',
        b"<html><body>Not a feed</body></html>",
    ],
)
def test_invalid_and_entity_feeds_fail_safely(body):
    with pytest.raises(worker.ParseError) as error:
        worker.feed_entries(body, deadline=deadline())
    assert isinstance(error.value, FetchError)
    assert error.value.status in {"unsupported", "extraction_failed"}
    assert "SECRET" not in str(error.value)
    assert "127.0.0.1" not in str(error.value)


def test_feed_entry_cap_rejects_instead_of_losing_matches():
    entry = "<item><link>https://example.com/a</link><description>Text</description></item>"
    assert len(worker.feed_entries(rss(entry * 200), deadline=deadline())) == 200
    with pytest.raises(worker.ParseError, match="limit") as error:
        worker.feed_entries(rss(entry * 201), deadline=deadline())
    assert error.value.status == "too_large"


@pytest.mark.parametrize("operation", ["article", "challenge"])
@pytest.mark.parametrize(
    "source",
    [
        (f"<main><h1>Report</h1><p>{TEXT}</p></main>" * 12000),
        "<div>" * 300 + ARTICLE + "</div>" * 300,
        "<span>x</span>" * 21000,
        "<main>" * 300 + ARTICLE + "</main>" * 300,
    ],
)
def test_hostile_html_is_bounded(operation, source):
    start = time.monotonic()
    with pytest.raises(worker.ParseError) as error:
        if operation == "article":
            worker.parse_article(source, URL, deadline=deadline(3))
        else:
            worker.check_challenge(source, deadline=deadline(3))
    assert error.value.status in {"too_large", "timeout"}
    assert time.monotonic() - start < 3.2


@pytest.mark.parametrize("source", [b"a" * (5 * 1024 * 1024 + 1), "😀" * (2 * 1024 * 1024)])
def test_input_bytes_cap(source):
    with pytest.raises(worker.ParseError) as error:
        worker.parse_article(source, URL, deadline=deadline())
    assert error.value.status == "too_large"
    assert URL not in str(error.value)
    assert "token" not in str(error.value)


def test_feed_body_and_nesting_limits():
    for body in (
        rss(
            "<item><description>" + "x" * (worker.MAX_FEED_BODY_BYTES + 1) + "</description></item>"
        ),
        rss("<item><description>" + "<b>" * 200 + "x" + "</b>" * 200 + "</description></item>"),
    ):
        with pytest.raises(worker.ParseError) as error:
            worker.feed_entries(body, deadline=deadline())
        assert error.value.status == "too_large"


def test_expired_deadline_and_invalid_arguments():
    with pytest.raises(worker.ParseError) as error:
        worker.parse_article(ARTICLE, URL, deadline=deadline(-1))
    assert error.value.status == "timeout"
    assert error.value.retryable
    for value in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            worker.check_challenge(ARTICLE, deadline=value)
    for value in (0, -1, True, 1.2):
        with pytest.raises(ValueError):
            worker.parse_article(ARTICLE, URL, deadline=deadline(), max_text_chars=value)


def _child(code):
    return subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={},
    )


def test_transport_stalled_stdin_is_killed_and_reaped():
    proc = _child("import time; time.sleep(30)")
    start = time.monotonic()
    with pytest.raises(worker.ParseError) as error:
        worker._exchange(proc, b"x" * (2 * 1024 * 1024), deadline(0.3))
    assert error.value.status == "timeout"
    assert proc.returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)
    assert time.monotonic() - start < 0.6


def test_transport_stdout_is_bounded_and_reaped():
    proc = _child("import os;\nwhile True: os.write(1, b'x' * 65536)")
    with pytest.raises(worker.ParseError) as error:
        worker._exchange(proc, b"", deadline(4))
    assert error.value.status == "too_large"
    assert proc.returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)


def test_repeated_actual_worker_timeouts_restore_capacity():
    with ThreadPoolExecutor(max_workers=8) as pool:

        def expire(_):
            with pytest.raises(worker.ParseError) as error:
                worker.parse_article(ARTICLE, URL, deadline=deadline(0.02))
            assert error.value.status == "timeout"

        list(pool.map(expire, range(16)))
    assert worker.parse_article(ARTICLE, URL, deadline=deadline()).text
    children = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").read_text().split()
    for child in children:
        command = Path(f"/proc/{child}/cmdline")
        if command.exists():
            assert b"parse_worker.py" not in command.read_bytes()


def test_child_only_resource_and_network_restrictions():
    before = resource.getrlimit(resource.RLIMIT_AS)
    code = f"""
import ctypes, importlib.util, json, os, resource, sys
sys.path.insert(0, {str(Path(worker.__file__).resolve().parent.parent)!r})
spec = importlib.util.spec_from_file_location("worker_sandbox_test", {worker.__file__!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module._sandbox()
libc = ctypes.CDLL(None, use_errno=True)
fd = libc.socket(2, 1, 0)
print(json.dumps({{"network_fd": fd, "errno": ctypes.get_errno(),
 "memory": resource.getrlimit(resource.RLIMIT_AS), "cpu": resource.getrlimit(resource.RLIMIT_CPU),
 "env": dict(os.environ)}}))
"""
    proc = _child(code)
    output, _ = proc.communicate(timeout=8)
    assert proc.returncode == 0
    result = json.loads(output)
    assert result["network_fd"] == -1
    assert result["errno"] == 1
    assert result["memory"] == [worker.MEMORY_BYTES] * 2
    assert result["cpu"][1] <= worker.CPU_SECONDS + 1
    assert resource.getrlimit(resource.RLIMIT_AS) == before


def test_worker_environment_is_allowlisted_without_mutating_parent(monkeypatch):
    monkeypatch.setenv("APP_SECRET_SENTINEL", "keep-only-in-parent")
    monkeypatch.setenv("HTTPS_PROXY", "http://not-for-worker.invalid")
    assert "APP_SECRET_SENTINEL" not in worker._worker_env()
    assert "HTTPS_PROXY" not in worker._worker_env()
    assert os.environ["APP_SECRET_SENTINEL"] == "keep-only-in-parent"
    assert worker.parse_article(ARTICLE, URL, deadline=deadline()).text


def test_legacy_extraction_runs_inside_bounded_worker():
    source = "<nav>Home News Subscribe</nav>" + ARTICLE
    result = worker.parse_article(source, URL, deadline=time.monotonic() + 5, legacy=True)
    assert result.method == "legacy" and result.completeness == "partial"
    assert "research group" in result.text
    assert "Home News Subscribe" not in result.text
    with pytest.raises(worker.ParseError):
        worker.parse_article("<main>" * 1000, URL, deadline=time.monotonic() + 5, legacy=True)
