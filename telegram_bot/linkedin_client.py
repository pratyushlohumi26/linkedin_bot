#!/usr/bin/env python3
"""LinkedIn API helper for publishing generated content."""

from __future__ import annotations

import json
import logging

import requests

logger = logging.getLogger(__name__)


class LinkedinAutomate:
    """Small LinkedIn publisher wrapper."""

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})

    def _get_user_id(self) -> str | None:
        url = "https://api.linkedin.com/v2/userinfo"
        try:
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            return resp.json().get("sub")
        except requests.RequestException as err:
            logger.error("Failed to fetch LinkedIn user ID: %s", err)
            return None

    def publish_post(self, description: str) -> requests.Response | None:
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
                data=json.dumps(payload),
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("LinkedIn post successful: %s", resp.status_code)
            return resp
        except requests.RequestException as err:
            logger.error("LinkedIn post failed: %s", err)
            return None
