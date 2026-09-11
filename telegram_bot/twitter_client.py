#!/usr/bin/env python3
"""X/Twitter posting helper using Tweepy."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping

import tweepy

from telegram_bot.config import XCredentials

logger = logging.getLogger(__name__)
MAX_TWEET_LENGTH = 280


class TwitterPublisher:
    """Posts thread content to X/Twitter."""

    def __init__(self, credentials: XCredentials):
        if not credentials.is_configured:
            raise ValueError("X credentials are not fully configured.")

        self._client = tweepy.Client(
            consumer_key=credentials.api_key,
            consumer_secret=credentials.api_secret_key,
            access_token=credentials.access_token,
            access_token_secret=credentials.access_token_secret,
        )

    def post_thread(self, tweet_thread: Mapping[int, str]) -> str:
        previous_tweet_id = None
        final_tweet_id = None
        ordered_tweets = sorted(tweet_thread.items(), key=lambda item: item[0])

        for index, tweet in ordered_tweets:
            text = f"[{index}/{len(ordered_tweets)}] {tweet.strip()}"
            if len(text) > MAX_TWEET_LENGTH:
                text = text[: MAX_TWEET_LENGTH - 3] + "..."

            kwargs = {"text": text}
            if previous_tweet_id:
                kwargs["in_reply_to_tweet_id"] = previous_tweet_id

            response = self._client.create_tweet(**kwargs)
            final_tweet_id = response.data["id"]
            previous_tweet_id = final_tweet_id
            time.sleep(0.5)

        if not final_tweet_id:
            raise ValueError("No tweet was created.")

        link = f"https://x.com/i/web/status/{final_tweet_id}"
        logger.info("Published X thread: %s", link)
        return link
