#!/usr/bin/env python3
"""LinkedIn API automation for posting user-generated content."""

import requests
import json
import re
import logging

from api_key import LINKEDIN_TOKEN

logger = logging.getLogger(__name__)

class LinkedinAutomate:
    """LinkedIn API automation for posting user-generated content."""
    def __init__(self, access_token=LINKEDIN_TOKEN, yt_url='', title='', description=''):
        """
        Initialize with LinkedIn access token and optional metadata.
        """
        self.access_token = access_token
        self.yt_url = yt_url
        self.title = title
        self.description = description
        self.python_group_list = [762547, 961087, 45655, 1814785]
        self.headers = {'Authorization': f'Bearer {self.access_token}'}
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def common_api_call_part(self, feed_type="feed", group_id=None):
        payload = {
            "author": f"urn:li:person:{self.user_id}",
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": self.description},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }
        return json.dumps(payload)

    def extract_thumbnail_url_from_YT_video_url(self):
        # exp = "^.*((youtu.be\/)|(v\/)|(\/u\/\w\/)|(embed\/)|(watch\?))\??v?=?([^#&?]*).*"
        s = re.findall(exp, self.yt_url)[0][-1]
        return f"https://i.ytimg.com/vi/{s}/maxresdefault.jpg"

    def get_user_id(self):
        """
        Retrieve the LinkedIn user ID (URN) for the authenticated user.
        """
        url = "https://api.linkedin.com/v2/userinfo"
        try:
            resp = self.session.get(url)
            resp.raise_for_status()
            data = resp.json()
            logger.debug("LinkedIn user info: %s", data)
            return data.get("sub")
        except requests.RequestException as err:
            logger.error("Failed to fetch LinkedIn user ID: %s", err)
            return None

    def feed_post(self):
        """
        Post content to user's LinkedIn feed.
        """
        url = "https://api.linkedin.com/v2/ugcPosts"
        payload = self.common_api_call_part()
        try:
            resp = self.session.post(url, data=payload)
            resp.raise_for_status()
            logger.info("LinkedIn feed post successful: %s", resp.status_code)
            return resp
        except requests.RequestException as err:
            logger.error("LinkedIn feed post failed: %s", err)
            return None

    def group_post(self, group_id):
        """
        Post content to a specified LinkedIn group.
        """
        url = "https://api.linkedin.com/v2/ugcPosts"
        payload = self.common_api_call_part(feed_type="group", group_id=group_id)
        try:
            resp = self.session.post(url, data=payload)
            resp.raise_for_status()
            logger.info("LinkedIn group post successful: %s", resp.status_code)
            return resp
        except requests.RequestException as err:
            logger.error("LinkedIn group post failed: %s", err)
            return None

    def main_func(self):
        self.user_id = self.get_user_id()
        return self.feed_post()