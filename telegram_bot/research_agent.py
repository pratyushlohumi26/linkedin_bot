#!/usr/bin/env python3
"""Optional web research helper for LinkedIn first-comment enrichment."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import requests

logger = logging.getLogger(__name__)


class ResearchAgent:
    """Fetches lightweight external references for follow-up LinkedIn comments."""

    def __init__(self, *, enabled: bool, provider: str, api_key: str | None, max_links: int = 3):
        self._enabled = enabled
        self._provider = provider.lower().strip()
        self._api_key = api_key
        self._max_links = max(1, min(max_links, 5))

    @property
    def is_ready(self) -> bool:
        return self._enabled and bool(self._api_key)

    def gather_references(self, *, topic: str) -> list[dict[str, str]]:
        if not self.is_ready:
            return []

        if self._provider != "tavily":
            logger.warning(
                "Unsupported SEARCH_PROVIDER=%s. Research enrichment skipped.", self._provider
            )
            return []

        try:
            response = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": self._api_key,
                    "query": topic,
                    "search_depth": "advanced",
                    "max_results": self._max_links,
                },
                timeout=20,
            )
            response.raise_for_status()
        except requests.RequestException as err:
            logger.warning("Research lookup failed: %s", err)
            return []

        payload = response.json()
        raw_results = payload.get("results", [])
        references: list[dict[str, str]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            title = str(item.get("title", "")).strip()
            snippet = str(item.get("content", "")).strip()
            if not url:
                continue
            references.append(
                {
                    "title": title or "Reference",
                    "url": url,
                    "snippet": snippet[:240],
                }
            )

        return references[: self._max_links]


def build_research_topic(article_text: str) -> str:
    """Create a compact search topic query from scraped article text."""
    lines: Sequence[str] = [line.strip() for line in article_text.splitlines() if line.strip()]
    if not lines:
        return "latest ai engineering research practical insights"
    head = " ".join(lines[:3])
    return f"{head[:220]} latest AI research"
