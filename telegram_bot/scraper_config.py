"""Validated, environment-backed settings for source ingestion."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ScraperConfig:
    mode: str = "layered"
    timeout_seconds: int = 10
    connect_timeout_seconds: int = 5
    total_timeout_seconds: int = 60
    max_attempts: int = 2
    max_response_bytes: int = 5 * 1024 * 1024
    max_redirects: int = 5
    max_text_chars: int = 60000
    min_interval_seconds: float = 0.5
    browser_enabled: bool = False
    browser_url: str = "http://scraper-browser:8787"
    browser_token: str | None = field(default=None, repr=False)
    browser_timeout_seconds: int = 25
    browser_max_concurrency: int = 1
    cache_ttl_seconds: int = 3600
    cache_path: str = ".runtime/scrape-cache.sqlite3"
    cache_max_entries: int = 128
    feed_urls: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self):
        if self.mode not in {"layered", "legacy"}:
            raise ValueError("SCRAPER_MODE must be layered or legacy.")
        bounds = {
            "timeout_seconds": (1, 60),
            "connect_timeout_seconds": (1, 30),
            "total_timeout_seconds": (5, 180),
            "max_attempts": (1, 3),
            "max_response_bytes": (1024, 10 * 1024 * 1024),
            "max_redirects": (0, 10),
            "max_text_chars": (1000, 100000),
            "browser_timeout_seconds": (1, 25),
            "browser_max_concurrency": (1, 1),
            "cache_ttl_seconds": (0, 86400),
            "cache_max_entries": (1, 1000),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"SCRAPER_{name.upper()} must be between {low} and {high}.")
        if not 0 <= self.min_interval_seconds <= 10:
            raise ValueError("SCRAPER_MIN_INTERVAL_SECONDS must be between 0 and 10.")
        if not self.cache_path.strip():
            raise ValueError("SCRAPER_CACHE_PATH must not be empty.")
        try:
            endpoint = urlsplit(self.browser_url)
            valid = (
                endpoint.scheme in {"http", "https"}
                and endpoint.hostname
                and endpoint.username is None
                and endpoint.password is None
                and endpoint.path in {"", "/"}
                and not endpoint.query
                and not endpoint.fragment
                and (endpoint.port is None or 1 <= endpoint.port <= 65535)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("SCRAPER_BROWSER_URL must be a trusted HTTP(S) service base URL.")
        if self.browser_enabled and (
            not self.browser_token
            or not 32 <= len(self.browser_token) <= 256
            or any(ord(c) < 33 or ord(c) > 126 for c in self.browser_token)
        ):
            raise ValueError(
                "SCRAPER_BROWSER_TOKEN must contain 32–256 printable non-space ASCII characters when rendering is enabled."
            )
        if self.browser_enabled and self.max_response_bytes > 5 * 1024 * 1024:
            raise ValueError("Browser rendering supports SCRAPER_MAX_RESPONSE_BYTES up to 5 MiB.")
        if len(self.feed_urls) > 20:
            raise ValueError("SCRAPER_FEED_URLS supports at most 20 publisher hosts.")
        for host, urls in self.feed_urls.items():
            if not isinstance(host, str) or host != host.lower() or not host or "/" in host:
                raise ValueError("SCRAPER_FEED_URLS keys must be lowercase publisher hostnames.")
            if not isinstance(urls, (list, tuple)) or not 1 <= len(urls) <= 2:
                raise ValueError("Each publisher must have one or two explicitly configured feeds.")
            for url in urls:
                if not isinstance(url, str):
                    raise ValueError("Feed URLs must be strings.")
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                ):
                    raise ValueError("Feed URLs must be public HTTP(S) URLs without credentials.")


def load_scraper_config(*, allowed_user_ids: set[int]) -> ScraperConfig:
    defaults = ScraperConfig()
    values = {}
    for name in (
        "timeout_seconds",
        "connect_timeout_seconds",
        "total_timeout_seconds",
        "max_attempts",
        "max_response_bytes",
        "max_redirects",
        "max_text_chars",
        "browser_timeout_seconds",
        "browser_max_concurrency",
        "cache_ttl_seconds",
        "cache_max_entries",
    ):
        try:
            values[name] = int(os.getenv("SCRAPER_" + name.upper(), str(getattr(defaults, name))))
        except ValueError:
            raise ValueError(f"SCRAPER_{name.upper()} must be an integer.") from None
    try:
        values["min_interval_seconds"] = float(os.getenv("SCRAPER_MIN_INTERVAL_SECONDS", "0.5"))
    except ValueError:
        raise ValueError("SCRAPER_MIN_INTERVAL_SECONDS must be a number.") from None
    enabled = os.getenv("SCRAPER_BROWSER_ENABLED", "false").strip().lower()
    if enabled not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        raise ValueError("SCRAPER_BROWSER_ENABLED must be a boolean.")
    values["browser_enabled"] = enabled in {"true", "1", "yes", "on"}
    if values["browser_enabled"] and not allowed_user_ids:
        raise ValueError("Set TELEGRAM_ALLOWED_USER_IDS before enabling browser rendering.")
    try:
        feeds = json.loads(os.getenv("SCRAPER_FEED_URLS", "{}"))
        if not isinstance(feeds, dict):
            raise ValueError
    except (ValueError, RecursionError):
        raise ValueError(
            "SCRAPER_FEED_URLS must be a JSON object mapping publisher hosts to feed URL lists."
        ) from None
    return ScraperConfig(
        **values,
        mode=os.getenv("SCRAPER_MODE", "layered").strip().lower(),
        browser_url=os.getenv("SCRAPER_BROWSER_URL", defaults.browser_url).strip().rstrip("/"),
        browser_token=os.getenv("SCRAPER_BROWSER_TOKEN") or None,
        cache_path=os.getenv("SCRAPER_CACHE_PATH", defaults.cache_path).strip(),
        feed_urls=feeds,
    )
