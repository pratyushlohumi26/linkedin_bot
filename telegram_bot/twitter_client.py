"""X publishing with the exact thread text shown during review."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence

import tweepy

from telegram_bot.config import XCredentials

logger = logging.getLogger(__name__)
MAX_TWEET_LENGTH = 280


def prepare_thread(tweet_thread: Mapping[int, str]) -> list[str]:
    if not tweet_thread:
        raise ValueError("No tweets were generated.")
    ordered = sorted(tweet_thread.items(), key=lambda item: int(item[0]))
    result = []
    for position, (_, text) in enumerate(ordered, 1):
        prefix = f"[{position}/{len(ordered)}] "
        budget = MAX_TWEET_LENGTH - len(prefix)
        body = text.strip()
        if len(body) > budget:
            body = body[: budget - 3].rstrip() + "..."
        result.append(prefix + body)
    return result


class TwitterPublisher:
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
        return self.post_prepared_thread(prepare_thread(tweet_thread))

    def post_prepared_thread(self, texts: Sequence[str]) -> str:
        if not texts or any(not text.strip() or len(text) > MAX_TWEET_LENGTH for text in texts):
            raise ValueError("The reviewed thread contains invalid tweet text.")
        previous_id = None
        for text in texts:
            kwargs = {"text": text}
            if previous_id:
                kwargs["in_reply_to_tweet_id"] = previous_id
            response = self._client.create_tweet(**kwargs)
            previous_id = response.data["id"]
            time.sleep(0.5)
        link = f"https://x.com/i/web/status/{previous_id}"
        logger.info("Published X thread: %s", link)
        return link
