"""Bounded source retrieval with honest outcomes and article provenance."""

from __future__ import annotations

import hashlib
import html
import logging
import time
from dataclasses import asdict, dataclass, replace
from threading import Lock
from urllib.parse import urlsplit
from urllib.request import parse_http_list
from urllib.robotparser import RobotFileParser

from telegram_bot.article_extractor import ExtractedArticle
from telegram_bot.http_fetcher import FetchError, SafeFetcher, validate_public_url
from telegram_bot.parse_worker import check_challenge, feed_entries, parse_article
from telegram_bot.scrape_cache import ScrapeCache
from telegram_bot.scraper_config import ScraperConfig

logger = logging.getLogger(__name__)
DEFAULT_HEADERS = {"User-Agent": "PublicArticleFetcher/1.0"}


@dataclass(frozen=True)
class ScrapeResult:
    status: str
    requested_url: str
    final_url: str = ""
    canonical_url: str = ""
    text: str = ""
    title: str = ""
    author: str = ""
    published_at: str = ""
    method: str = "http"
    completeness: str = "unknown"
    warnings: tuple[str, ...] = ()
    content_hash: str = ""
    error: str = ""
    retryable: bool = False
    elapsed_seconds: float = 0
    attempts: int = 0
    cached: bool = False

    @property
    def usable(self) -> bool:
        return self.status in {"success", "partial"} and bool(self.text.strip())

    def metadata(self) -> dict:
        data = asdict(self)
        data.pop("text")
        return data


def source_result(
    article: ExtractedArticle, url: str, *, final_url: str = "", method: str = "http"
) -> ScrapeResult:
    status = "success" if article.completeness == "likely_complete" else "partial"
    return ScrapeResult(
        status=status if article.text else "extraction_failed",
        requested_url=url,
        final_url=final_url or url,
        canonical_url=article.canonical_url,
        text=article.text,
        title=article.title,
        author=article.author,
        published_at=article.published_at,
        method=method + ":" + article.method,
        completeness=article.completeness,
        warnings=article.warnings,
        content_hash=hashlib.sha256(article.text.encode()).hexdigest(),
        error=(
            ""
            if article.text
            else "No usable article body was found. Paste the article text or use another source."
        ),
    )


class ArticleScraper:
    def __init__(self, config: ScraperConfig, *, fetcher=None, renderer=None):
        self.config = config
        self.fetcher = fetcher or SafeFetcher(config)
        self.renderer = renderer
        self._robots = {}
        self._robots_lock = Lock()
        self.cache = None
        if config.cache_ttl_seconds:
            try:
                self.cache = ScrapeCache(config.cache_path, config.cache_max_entries)
            except Exception:
                logger.warning("Scraper cache unavailable; continuing without cache")

    def _robots_allowed(self, url: str, deadline: float):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            cached = self._robots.get(origin)
        if cached and cached[0] > time.monotonic():
            parser = cached[1]
        else:
            response = self.fetcher.fetch(origin + "/robots.txt", deadline=deadline)
            if response.status_code in {401, 403} or check_challenge(
                response.body, response.headers, deadline=deadline
            ):
                raise FetchError(
                    "blocked",
                    "The publisher blocked automated access. Paste the article text or upload a .txt file.",
                    False,
                )
            if response.status_code >= 500 or response.status_code == 429:
                raise FetchError(
                    "fetch_failed",
                    "Publisher access guidance is temporarily unavailable. Retry later or paste the text.",
                    True,
                )
            parser = RobotFileParser()
            if response.status_code in {404, 410}:
                parser.parse([])
            elif response.status_code == 200 and len(response.body) <= 512 * 1024:
                parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
            else:
                raise FetchError(
                    "blocked",
                    "Publisher access guidance could not be validated. Please paste the article text.",
                    False,
                )
            with self._robots_lock:
                if len(self._robots) >= 128:
                    self._robots.pop(next(iter(self._robots)))
                self._robots[origin] = (time.monotonic() + 3600, parser)
        if not parser.can_fetch("PublicArticleFetcher", url):
            raise FetchError(
                "blocked",
                "The publisher's robots policy does not permit fetching this page. Please supply the text yourself.",
                False,
            )

        delay = parser.crawl_delay("PublicArticleFetcher") or 0
        rate = parser.request_rate("PublicArticleFetcher")
        if rate and rate.requests:
            delay = max(delay, rate.seconds / rate.requests)
        if delay:
            if time.monotonic() + delay >= deadline:
                raise FetchError(
                    "rate_limited",
                    "Publisher pacing exceeds this request's time budget. Try later or supply the text.",
                    True,
                )
            time.sleep(delay)
        return delay

    def _fetch(self, url: str, deadline: float):
        return self.fetcher.fetch(url, deadline=deadline, before_request=self._robots_allowed)

    def _extract(self, response, requested_url: str, deadline: float) -> ScrapeResult:
        if response.status_code in {401, 403} or check_challenge(
            response.body, response.headers, deadline=deadline
        ):
            return ScrapeResult(
                "blocked",
                requested_url,
                response.url,
                error="This site blocked automated fetching. The article body was not retrieved; paste the text or upload a .txt file.",
            )
        if response.status_code in {404, 410}:
            return ScrapeResult(
                "not_found",
                requested_url,
                response.url,
                error="The article was not found. Check the URL or paste the text.",
            )
        if response.status_code == 429:
            return ScrapeResult(
                "rate_limited",
                requested_url,
                response.url,
                error="The publisher is rate-limiting requests. Wait before retrying, or paste the text.",
                retryable=True,
            )
        if not 200 <= response.status_code < 300:
            return ScrapeResult(
                "fetch_failed",
                requested_url,
                response.url,
                error="The publisher did not return a usable page. Retry later or supply the text.",
                retryable=response.status_code >= 500,
            )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            return ScrapeResult(
                "unsupported",
                requested_url,
                response.url,
                error="That URL is not an HTML article. Paste text or upload a UTF-8 .txt file.",
            )
        article = parse_article(
            response.body,
            response.url,
            max_text_chars=self.config.max_text_chars,
            deadline=deadline,
            legacy=self.config.mode == "legacy",
        )
        method = "http"
        if article.needs_render and self.config.browser_enabled:
            if self.renderer is None:
                from telegram_bot.browser_reader import BrowserReader

                self.renderer = BrowserReader(self.config)
            try:
                rendered = self.renderer.render(response.url, deadline=deadline)
                if check_challenge(rendered, deadline=deadline):
                    return ScrapeResult(
                        "blocked",
                        requested_url,
                        response.url,
                        error="The rendered page still requires an access challenge. Please paste the article text.",
                        method="browser",
                    )
                article = parse_article(
                    rendered,
                    response.url,
                    max_text_chars=self.config.max_text_chars,
                    deadline=deadline,
                )
                method = "browser"
            except Exception:
                return ScrapeResult(
                    "extraction_failed",
                    requested_url,
                    response.url,
                    error="Browser rendering could not retrieve the article. Paste the text or use another source.",
                    method="browser",
                )
        elif article.needs_render:
            return ScrapeResult(
                "extraction_failed",
                requested_url,
                response.url,
                error="This page appears to need JavaScript rendering, which is disabled. Paste its text or upload a .txt file.",
            )
        return source_result(article, requested_url, final_url=response.url, method=method)

    @staticmethod
    def _identity(url: str) -> str:
        return validate_public_url(url)

    def _feed_fallback(self, url: str, deadline: float) -> ScrapeResult | None:
        host = urlsplit(url).hostname
        for feed_url in self.config.feed_urls.get(host, ()):
            try:
                response = self._fetch(validate_public_url(feed_url), deadline)
                if (
                    response.status_code != 200
                    or check_challenge(response.body, response.headers, deadline=deadline)
                    or b"<!doctype" in response.body.lower()
                ):
                    continue
                for item in feed_entries(response.body, deadline=deadline):
                    if not item["link"] or self._identity(item["link"].strip()) != self._identity(
                        url
                    ):
                        continue
                    article = parse_article(
                        f"<article><h1>{html.escape(item['title'])}</h1>{item['body']}</article>",
                        url,
                        max_text_chars=self.config.max_text_chars,
                        deadline=deadline,
                    )
                    if not article.text:
                        continue
                    if item["body_kind"] == "description":
                        article = replace(
                            article,
                            completeness="partial",
                            warnings=(
                                *article.warnings,
                                "Publisher feed supplies a description/summary, not a verified full article.",
                            ),
                        )
                    return source_result(
                        article, url, final_url=response.url, method="publisher_feed"
                    )
            except Exception as exc:
                logger.warning("Optional publisher feed unavailable (%s)", type(exc).__name__)
                continue
        return None

    def scrape(self, url: str) -> ScrapeResult:
        start = time.monotonic()
        deadline = start + self.config.total_timeout_seconds
        attempts = 0
        try:
            url = validate_public_url(url)
            cache_key = hashlib.sha256(
                f"v1|{self.config.mode}|{self.config.max_text_chars}|{url}".encode()
            ).hexdigest()
            if self.cache and not urlsplit(url).query:
                try:
                    cached = self.cache.get(cache_key)
                    if cached:
                        cached["warnings"] = tuple(cached.get("warnings", ()))
                        return replace(
                            ScrapeResult(**cached),
                            cached=True,
                            elapsed_seconds=round(time.monotonic() - start, 3),
                        )
                except Exception:
                    logger.warning("Scraper cache read failed; fetching normally")
            response = self._fetch(url, deadline)
            attempts = response.attempts
            result = self._extract(response, url, deadline)
            cache_control = response.headers.get("cache-control", "").lower()
            directives = parse_http_list(cache_control)
            directive_names = {part.split("=", 1)[0].strip() for part in directives}
            cacheable = (
                result.status == "success"
                and result.method.startswith("http:")
                and "public" in directives
                and not directive_names.intersection({"private", "no-store", "no-cache"})
                and "set-cookie" not in response.headers
                and not urlsplit(url).query
                and not urlsplit(response.url).query
                and "vary" not in response.headers
            )
            if self.cache and cacheable:
                try:
                    lifetimes = [
                        int(part.split("=", 1)[1].strip().strip('"'))
                        for part in directives
                        if part.split("=", 1)[0].strip() in {"max-age", "s-maxage"}
                    ]
                    age = int(response.headers.get("age", "0"))
                    if lifetimes and age >= 0:
                        ttl = min(self.config.cache_ttl_seconds, min(lifetimes) - age)
                        if ttl > 0:
                            self.cache.put(cache_key, asdict(result), ttl)
                except Exception:
                    logger.warning("Scraper cache write failed; result remains usable")
        except FetchError as exc:
            result = ScrapeResult(exc.status, url, error=exc.message, retryable=exc.retryable)
        except Exception as exc:
            logger.warning("Source retrieval failed (%s)", type(exc).__name__)
            result = ScrapeResult(
                "fetch_failed",
                url,
                error="Could not retrieve a usable article. Please paste its text or use another URL.",
            )
        if (
            result.status in {"blocked", "extraction_failed", "partial"}
            and time.monotonic() < deadline
        ):
            alternate = self._feed_fallback(url, deadline)
            if alternate and (not result.usable or alternate.status == "success"):
                result = alternate
        if time.monotonic() > deadline:
            return ScrapeResult(
                "timeout",
                url,
                error="Source retrieval exceeded its time budget. Retry later or paste the text.",
                retryable=True,
                elapsed_seconds=round(time.monotonic() - start, 3),
                attempts=attempts,
            )
        return replace(
            result, elapsed_seconds=round(time.monotonic() - start, 3), attempts=attempts
        )


def extract_text_from_url(url: str, *, timeout_seconds: int = 10) -> str | None:
    """Compatibility wrapper; all fetching still uses public-network safety checks."""
    result = ArticleScraper(
        ScraperConfig(timeout_seconds=timeout_seconds, cache_ttl_seconds=0)
    ).scrape(url)
    return result.text if result.usable else None
