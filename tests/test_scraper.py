"""Real classification/extraction/cache logic; deterministic HTTP boundary avoids live sites."""

import time
from types import SimpleNamespace

import pytest
from test_http_fetcher import network as network
from test_http_fetcher import reply

from telegram_bot.http_fetcher import FetchError
from telegram_bot.scraper import ArticleScraper
from telegram_bot.scraper_config import ScraperConfig

URL = "https://articles.example.org/story"
ARTICLE = """<html><head><title>Local model deployment</title></head><body>
<nav>Home News Subscribe About Contact Privacy</nav><article><h1>Local model deployment</h1>
<p>Engineers can run a compact language model on a laptop without sending every input to a remote service. This design can reduce network dependence and protect sensitive prompts when the application keeps its processing local.</p>
<p>The team measured memory usage and response latency before choosing a deployment configuration. Quantization reduced the space needed for model weights, but its effect on answer quality required separate evaluation against representative tasks.</p>
<p>For production use, operators should compare these trade-offs with their own workloads. A smaller footprint does not automatically make a model accurate, and offline inference still needs careful testing and update management.</p>
</article><footer>Subscribe to more stories and related content.</footer></body></html>"""
CHALLENGE = b"<html><title>Just a moment...</title><body>Enable JavaScript and cookies to continue</body></html>"


class HTTPBoundary:
    def __init__(self, body=ARTICLE, status=200, headers=None, robots=b"User-agent: *\nAllow: /\n"):
        self.body = body.encode() if isinstance(body, str) else body
        self.status = status
        self.headers = {"content-type": "text/html", **(headers or {})}
        self.robots = robots
        self.calls = []
        self.routes = {}

    def fetch(self, url, *, deadline=None, before_request=None):
        if before_request:
            before_request(url, deadline)
        self.calls.append(url)
        status, body, headers = self.routes.get(url, (self.status, self.body, self.headers))
        if url.endswith("/robots.txt"):
            status, body, headers = 200, self.robots, {"content-type": "text/plain"}
        return SimpleNamespace(
            url=url,
            requested_url=url,
            status_code=status,
            body=body,
            headers=headers,
            attempts=1,
            elapsed_seconds=0.01,
        )


@pytest.fixture
def make_scraper(tmp_path):
    def make(boundary=None, **settings):
        config = ScraperConfig(cache_path=str(tmp_path / "cache.sqlite3"), **settings)
        return ArticleScraper(config, fetcher=boundary or HTTPBoundary())

    return make


def test_extracts_article_and_records_provenance(make_scraper):
    result = make_scraper().scrape(URL)
    assert result.usable
    assert "Quantization reduced" in result.text
    assert "Home News Subscribe" not in result.text
    assert result.title == "Local model deployment"
    assert result.requested_url == URL == result.final_url
    assert len(result.content_hash) == 64
    assert "text" not in result.metadata()


@pytest.mark.parametrize("status", [200, 403])
def test_challenge_never_reaches_renderer_or_becomes_article(make_scraper, status):
    boundary = HTTPBoundary(CHALLENGE, status=status, headers={"cf-mitigated": "challenge"})
    scraper = make_scraper(boundary, browser_enabled=True, browser_token="x" * 32)

    class Renderer:
        def render(self, *args, **kwargs):
            pytest.fail("An access challenge must not trigger browser escalation")

    scraper.renderer = Renderer()
    result = scraper.scrape(URL)
    assert result.status == "blocked"
    assert not result.usable and not result.text
    assert boundary.calls.count(URL) == 1


def test_robots_disallow_stops_article_fetch(make_scraper):
    boundary = HTTPBoundary(robots=b"User-agent: *\nDisallow: /story\n")
    result = make_scraper(boundary).scrape(URL)
    assert result.status == "blocked"
    assert URL not in boundary.calls


@pytest.mark.parametrize(
    "status, expected, retryable",
    [(404, "not_found", False), (429, "rate_limited", True), (503, "fetch_failed", True)],
)
def test_http_statuses_are_actionable(make_scraper, status, expected, retryable):
    result = make_scraper(HTTPBoundary(status=status)).scrape(URL)
    assert result.status == expected
    assert result.retryable is retryable
    assert not result.usable


def test_non_html_is_not_an_article(make_scraper):
    result = make_scraper(
        HTTPBoundary(b"%PDF-test", headers={"content-type": "application/pdf"})
    ).scrape(URL)
    assert result.status == "unsupported"


def test_js_shell_only_renders_when_enabled(make_scraper):
    boundary = HTTPBoundary(
        '<html><body><div id="root"></div><script src="app.js"></script></body></html>'
    )
    disabled = make_scraper(boundary).scrape(URL)
    assert not disabled.usable and "JavaScript" in disabled.error
    scraper = make_scraper(boundary, browser_enabled=True, browser_token="x" * 32)

    class Renderer:
        def render(self, url, *, deadline):
            assert url == URL and deadline > 0
            return ARTICLE.encode()

    scraper.renderer = Renderer()
    rendered = scraper.scrape(URL)
    assert rendered.usable and rendered.method.startswith("browser:")


def test_browser_failure_is_safe(make_scraper):
    boundary = HTTPBoundary('<div id="root"></div><script src="app.js"></script>')
    scraper = make_scraper(boundary, browser_enabled=True, browser_token="x" * 32)

    class Renderer:
        def render(self, *args, **kwargs):
            raise RuntimeError("private-token-and-provider-body")

    scraper.renderer = Renderer()
    result = scraper.scrape(URL)
    assert not result.usable and "private-token" not in result.error


def test_cache_only_stores_explicitly_public_success(make_scraper):
    boundary = HTTPBoundary(headers={"cache-control": "public, max-age=1000"})
    scraper = make_scraper(boundary)
    first = scraper.scrape(URL)
    assert first.status == "success"
    count = len(boundary.calls)
    second = scraper.scrape(URL)
    assert second.cached and second.text == first.text
    assert len(boundary.calls) == count


@pytest.mark.parametrize(
    "headers, suffix",
    [
        ({}, ""),
        ({"cache-control": "private"}, ""),
        ({"cache-control": "public", "set-cookie": "session=value"}, ""),
        ({"cache-control": "public"}, "?signed=private"),
    ],
)
def test_private_or_unspecified_responses_not_cached(make_scraper, headers, suffix):
    boundary = HTTPBoundary(headers=headers)
    scraper = make_scraper(boundary)
    scraper.scrape(URL + suffix)
    assert not scraper.scrape(URL + suffix).cached
    assert boundary.calls.count(URL + suffix) == 2


def test_feed_matches_exact_article_and_labels_description_partial(make_scraper):
    feed = "https://articles.example.org/feed.xml"
    boundary = HTTPBoundary(CHALLENGE, 403)
    body = f"<rss><channel><item><title>Local model deployment</title><link>{URL}</link><description><![CDATA[{ARTICLE}]]></description></item></channel></rss>".encode()
    boundary.routes[feed] = (200, body, {"content-type": "application/rss+xml"})
    scraper = make_scraper(boundary, feed_urls={"articles.example.org": (feed,)})
    result = scraper.scrape(URL)
    assert result.usable and result.status == "partial"
    assert result.method.startswith("publisher_feed:")
    assert result.final_url == feed and result.requested_url == URL
    assert any("description" in warning for warning in result.warnings)


def test_feed_does_not_substitute_another_story(make_scraper):
    feed = "https://articles.example.org/feed.xml"
    boundary = HTTPBoundary(CHALLENGE, 403)
    boundary.routes[feed] = (
        200,
        f"<rss><channel><item><link>{URL}-other</link><description><![CDATA[{ARTICLE}]]></description></item></channel></rss>".encode(),
        {"content-type": "application/rss+xml"},
    )
    result = make_scraper(boundary, feed_urls={"articles.example.org": (feed,)}).scrape(URL)
    assert result.status == "blocked" and not result.text


def test_fetch_errors_do_not_leak_provider_details(make_scraper):
    class Unavailable:
        def fetch(self, *args, **kwargs):
            raise FetchError("timeout", "The request timed out.", True)

    result = make_scraper(Unavailable()).scrape(URL)
    assert result.status == "timeout" and result.retryable


def test_invalid_url_never_fetches(make_scraper):
    boundary = HTTPBoundary()
    result = make_scraper(boundary).scrape("http://127.0.0.1/secrets")
    assert result.status in {"invalid_url", "blocked"} and not boundary.calls


def test_cache_capacity_is_bounded(make_scraper):
    scraper = make_scraper(cache_max_entries=2)
    for i in range(4):
        scraper.cache.put(str(i), {"value": i}, 100)
    assert scraper.cache.get("0") is None
    assert scraper.cache.get("3") == {"value": 3}


def test_cached_expired_entry_is_removed(make_scraper):
    scraper = make_scraper()
    scraper.cache.put("expired", {"value": 1}, -1)
    assert scraper.cache.get("expired") is None


def test_legacy_mode_retains_network_and_challenge_guards(make_scraper):
    scraper = make_scraper(HTTPBoundary(CHALLENGE, 200), mode="legacy")
    assert scraper.scrape(URL).status == "blocked"
    scraper = make_scraper(mode="legacy")
    result = scraper.scrape(URL)
    assert result.usable and result.status == "partial"


def test_specific_bot_policy_uses_actual_http_user_agent(make_scraper):
    boundary = HTTPBoundary(
        robots=b"User-agent: PublicArticleFetcher\nDisallow: /story\n\nUser-agent: *\nAllow: /\n"
    )
    result = make_scraper(boundary).scrape(URL)
    assert result.status == "blocked"
    assert URL not in boundary.calls


def test_failed_optional_feed_does_not_erase_primary_result(make_scraper):
    boundary = HTTPBoundary(CHALLENGE, 403)
    feed = "https://articles.example.org/feed.xml"
    original = boundary.fetch

    def fetch(url, *, deadline=None, before_request=None):
        if url == feed:
            raise RuntimeError("unexpected-parser-or-transport-error")
        return original(url, deadline=deadline, before_request=before_request)

    boundary.fetch = fetch
    result = make_scraper(boundary, feed_urls={"articles.example.org": (feed,)}).scrape(URL)
    assert result.status == "blocked" and not result.usable


def test_real_http_facade_checks_policy_before_following_redirect(network, tmp_path):
    def respond(handler):
        if handler.path == "/robots.txt":
            reply(
                handler,
                body=b"User-agent: PublicArticleFetcher\nDisallow: /forbidden\n",
                headers={"Content-Type": "text/plain"},
            )
        else:
            reply(handler, status=302, headers={"Location": "/forbidden"})

    network.serve(respond)
    scraper = ArticleScraper(ScraperConfig(cache_ttl_seconds=0, min_interval_seconds=0))
    result = scraper.scrape("http://articles.example.org/start")
    assert result.status == "blocked"
    assert [path for path, _, _ in network.records] == ["/robots.txt", "/start"]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("private_header", ["Cache-Control", "Vary", "Set-Cookie"])
def test_duplicate_policy_headers_never_allow_shared_cache(
    network, tmp_path, reverse, private_header
):
    def respond(handler):
        handler.send_response(200)
        if handler.path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            handler.send_header("Content-Type", "text/plain")
        else:
            body = ARTICLE.encode()
            handler.send_header("Content-Type", "text/html")
            values = {
                "Cache-Control": ["private, no-store", "public, max-age=60"],
                "Vary": ["Cookie", ""],
                "Set-Cookie": ["session=private", ""],
            }[private_header]
            if reverse:
                values = list(reversed(values))
            if private_header != "Cache-Control":
                handler.send_header("Cache-Control", "public, max-age=60")
            for value in values:
                handler.send_header(private_header, value)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    network.serve(respond)
    scraper = ArticleScraper(
        ScraperConfig(cache_path=str(tmp_path / "cache.sqlite3"), min_interval_seconds=0)
    )
    url = "http://articles.example.org/story"
    assert scraper.scrape(url).usable
    assert not scraper.scrape(url).cached
    assert [path for path, _, _ in network.records].count("/story") == 2


@pytest.mark.parametrize(
    "directive",
    ["x-public, max-age=60", 'extension="x,public", max-age=60', "public=invalid, max-age=60"],
)
def test_cache_requires_actual_public_directive(make_scraper, directive):
    scraper = make_scraper(HTTPBoundary(headers={"cache-control": directive}))
    assert scraper.scrape(URL).usable
    assert not scraper.scrape(URL).cached


@pytest.mark.parametrize(
    "other", [URL + "/", URL.replace("https:", "http:"), URL.replace(".org/", ".org:80/")]
)
def test_feed_requires_exact_resource_identity(make_scraper, other):
    feed = "https://articles.example.org/feed.xml"
    boundary = HTTPBoundary(CHALLENGE, 403)
    boundary.routes[feed] = (
        200,
        f"<rss><channel><item><link>{other}</link><description><![CDATA[{ARTICLE}]]></description></item></channel></rss>".encode(),
        {"content-type": "application/rss+xml"},
    )
    result = make_scraper(boundary, feed_urls={"articles.example.org": (feed,)}).scrape(URL)
    assert result.status == "blocked" and not result.text


@pytest.mark.parametrize(
    "headers",
    [
        {"cache-control": "public, max-age=60, max-age=0"},
        {"cache-control": "public, max-age=60, s-maxage=0"},
        {"cache-control": "public, max-age=60", "age": "60"},
        {"cache-control": "public"},
    ],
)
def test_shared_cache_does_not_extend_publisher_freshness(make_scraper, headers):
    scraper = make_scraper(HTTPBoundary(headers=headers))
    assert scraper.scrape(URL).usable
    assert not scraper.scrape(URL).cached


@pytest.mark.parametrize(
    "body",
    [
        "<main><h1>A workshop experiment</h1><p>A small research group tested a new cooling system in its workshop. The first experiment reduced heat without increasing the machine power demand.</p></main>"
        * 12000,
        "<div>" * 1000 + ARTICLE + "</div>" * 1000,
    ],
)
def test_complex_html_is_rejected_within_full_scrape_deadline(make_scraper, body):
    scraper = make_scraper(HTTPBoundary(body), total_timeout_seconds=5)
    started = time.monotonic()
    result = scraper.scrape(URL)
    assert result.status == "too_large" and not result.usable
    assert time.monotonic() - started < 5
    scraper.fetcher = HTTPBoundary()
    assert scraper.scrape(URL).usable
