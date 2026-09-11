#!/usr/bin/env python3
"""LinkedIn API helper for publishing generated content."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote, unquote

import requests

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LinkedInPublishResult:
    status_code: int
    post_urn: str | None


class LinkedinAutomate:
    """Small LinkedIn publisher wrapper."""

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "X-Restli-Protocol-Version": "2.0.0",
            }
        )

    def _get_user_id(self) -> str | None:
        url = "https://api.linkedin.com/v2/userinfo"
        try:
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            return resp.json().get("sub")
        except requests.RequestException as err:
            logger.error("Failed to fetch LinkedIn user ID: %s", err)
            return None

    def _extract_post_urn(self, response: requests.Response) -> str | None:
        candidates: list[str] = []

        header_id = response.headers.get("x-restli-id")
        if header_id:
            candidates.append(header_id)

        location = response.headers.get("location")
        if location:
            candidates.append(location.rsplit("/", 1)[-1])

        try:
            payload = response.json()
            if isinstance(payload, dict):
                post_id = payload.get("id")
                if isinstance(post_id, str):
                    candidates.append(post_id)
        except ValueError:
            pass

        for candidate in candidates:
            decoded = unquote(candidate).strip()
            if decoded.startswith("urn:li:"):
                return decoded
            if decoded.isdigit():
                return f"urn:li:ugcPost:{decoded}"

        return None

    def publish_post(self, description: str) -> LinkedInPublishResult | None:
        user_id = self._get_user_id()
        if not user_id:
            return None

        payload = {
            "author": f"urn:li:person:{user_id}",
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": description},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }

        try:
            resp = self.session.post(
                "https://api.linkedin.com/v2/ugcPosts",
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            post_urn = self._extract_post_urn(resp)
            logger.info("LinkedIn post successful: %s post_urn=%s", resp.status_code, post_urn)
            return LinkedInPublishResult(status_code=resp.status_code, post_urn=post_urn)
        except requests.RequestException as err:
            logger.error("LinkedIn post failed: %s", err)
            return None

    def post_comment(self, *, post_urn: str, comment_text: str) -> requests.Response | None:
        user_id = self._get_user_id()
        if not user_id:
            return None

        encoded_post_urn = quote(post_urn, safe="")
        payload = {
            "actor": f"urn:li:person:{user_id}",
            "message": {"text": comment_text},
        }

        try:
            response = self.session.post(
                f"https://api.linkedin.com/v2/socialActions/{encoded_post_urn}/comments",
                json=payload,
                timeout=15,
            )
            response.raise_for_status()
            logger.info("LinkedIn first comment posted successfully for %s", post_urn)
            return response
        except requests.RequestException as err:
            logger.error("LinkedIn first comment failed: %s", err)
            return None
