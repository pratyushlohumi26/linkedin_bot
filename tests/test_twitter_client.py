"""Exercise actual thread preparation without contacting X."""

import pytest

from telegram_bot.twitter_client import MAX_TWEET_LENGTH, prepare_thread


def test_prepare_thread_adds_only_numbering_to_ordered_text():
    assert prepare_thread({2: " Second point. ", 1: "First point."}) == [
        "[1/2] First point.",
        "[2/2] Second point.",
    ]


def test_prepare_thread_preserves_legitimate_tool_references():
    text = "OpenHands is an AI agent for software development."
    assert prepare_thread({1: text}) == ["[1/1] " + text]


def test_final_tweet_uses_full_remaining_character_budget():
    prefix = "[1/1] "
    text = "x" * (MAX_TWEET_LENGTH - len(prefix))
    assert prepare_thread({1: text}) == [prefix + text]


def test_long_tweets_remain_bounded_with_numbering():
    result = prepare_thread({i: "x" * 400 for i in range(1, 13)})
    assert len(result) == 12
    for i, text in enumerate(result, 1):
        assert text.startswith(f"[{i}/12] ")
        assert text.endswith("...")
        assert len(text) == MAX_TWEET_LENGTH


def test_prepare_thread_rejects_empty_input():
    with pytest.raises(ValueError, match="No tweets"):
        prepare_thread({})
