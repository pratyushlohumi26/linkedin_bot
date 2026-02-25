#!/usr/bin/env python3
"""Twitter posting helper using Tweepy."""

import time
import logging

import tweepy
from api_key import x_api_key, x_api_secret_key, x_access_token, x_access_token_secret

logger = logging.getLogger(__name__)

def post_twitter(tweet_thread: dict):
    """Post a sequence of tweets (thread) and return the final tweet URL."""
    client = tweepy.Client(
        consumer_key=x_api_key,
        consumer_secret=x_api_secret_key,
        access_token=x_access_token,
        access_token_secret=x_access_token_secret,
    )
    for i, tweet in tweet_thread.items():
        thread = f"[{i}/{len(tweet_thread)}] " + tweet
        print("--> ", thread)
        response = client.create_tweet(text=thread)
        time.sleep(0.5)
    try:
        tweet_id = response.data['id']
        twitter_link = f"https://x.com/PratyushLohumi/status/{tweet_id}"
        return twitter_link
    except Exception as e:
        return e