#!/usr/bin/env python3
"""Content scraping helper: fetch and clean text from a URL."""

from __future__ import annotations

import logging

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    )
}


def extract_text_from_url(url: str, *, timeout_seconds: int = 10) -> str | None:
    """Fetch HTML content at `url` and return cleaned plaintext."""
    try:
        resp = requests.get(url, timeout=timeout_seconds, headers=DEFAULT_HEADERS)
        resp.raise_for_status()
    except requests.RequestException as err:
        logger.error("Error fetching URL %s: %s", url, err)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    chunks = [phrase.strip() for line in lines for phrase in line.split("  ")]
    cleaned = "\n".join(chunk for chunk in chunks if chunk)
    return cleaned or None
