from __future__ import annotations

import requests

from telegram_bot.linkedin_client import LinkedinAutomate


def _response_with(
    *, headers: dict[str, str] | None = None, payload: str | None = None
) -> requests.Response:
    response = requests.Response()
    response.status_code = 201
    response.headers.update(headers or {})
    if payload is not None:
        response._content = payload.encode("utf-8")
        response.headers.setdefault("Content-Type", "application/json")
    return response


def test_extract_post_urn_from_header() -> None:
    client = LinkedinAutomate("dummy")
    response = _response_with(headers={"x-restli-id": "urn:li:ugcPost:123"})

    assert client._extract_post_urn(response) == "urn:li:ugcPost:123"


def test_extract_post_urn_from_numeric_location() -> None:
    client = LinkedinAutomate("dummy")
    response = _response_with(headers={"location": "https://api.linkedin.com/v2/ugcPosts/98765"})

    assert client._extract_post_urn(response) == "urn:li:ugcPost:98765"


def test_extract_post_urn_from_payload() -> None:
    client = LinkedinAutomate("dummy")
    response = _response_with(payload='{"id": "urn:li:ugcPost:111"}')

    assert client._extract_post_urn(response) == "urn:li:ugcPost:111"
