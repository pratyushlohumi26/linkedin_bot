#!/usr/bin/env python3
"""Content scraping helper: fetch and clean text from a URL."""

import requests
from bs4 import BeautifulSoup
import logging

logger = logging.getLogger(__name__)

def extract_text_from_url(url):
    """
    Fetches the HTML content at `url` and returns cleaned plaintext.
    Strips scripts/styles and collapses whitespace.
    """
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        # Normalize whitespace
        lines = [line.strip() for line in text.splitlines()]
        chunks = [phrase.strip() for line in lines for phrase in line.split("  ")]
        cleaned = "\n".join(chunk for chunk in chunks if chunk)
        return cleaned
    except requests.RequestException as err:
        logger.error("Error fetching URL %s: %s", url, err)
        return None