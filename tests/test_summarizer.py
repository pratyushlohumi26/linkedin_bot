from __future__ import annotations

import pytest

from telegram_bot.summarizer import (
    _apply_hashtag_policy,
    _parse_linkedin_variants,
    _parse_thread_payload,
)


def test_parse_thread_payload_json() -> None:
    payload = '{"1": "tweet one", "2": "tweet two"}'
    assert _parse_thread_payload(payload) == {1: "tweet one", 2: "tweet two"}


def test_parse_thread_payload_python_dict_literal() -> None:
    payload = "{1: 'tweet one', 2: 'tweet two'}"
    assert _parse_thread_payload(payload) == {1: "tweet one", 2: "tweet two"}


def test_parse_thread_payload_markdown_json_block() -> None:
    payload = """```json
    {"2": "second", "1": "first"}
    ```"""
    assert _parse_thread_payload(payload) == {1: "first", 2: "second"}


def test_parse_thread_payload_rejects_non_dict() -> None:
    with pytest.raises(ValueError, match="dictionary-like"):
        _parse_thread_payload("['a', 'b']")


def test_parse_thread_payload_rejects_empty_tweet() -> None:
    with pytest.raises(ValueError, match="Invalid tweet content"):
        _parse_thread_payload('{"1": "   "}')


def test_parse_linkedin_variants_json_payload() -> None:
    payload = '{"A":"Post A","B":"Post B","C":"Post C"}'
    assert _parse_linkedin_variants(payload) == {
        "A": "Post A",
        "B": "Post B",
        "C": "Post C",
    }


def test_parse_linkedin_variants_rejects_missing_variant() -> None:
    with pytest.raises(ValueError, match="variant"):
        _parse_linkedin_variants('{"A":"one","B":"two"}')


def test_apply_hashtag_policy_enforces_core_and_limit() -> None:
    post = "Line one\nLine two #Random #AIAgents #TooMany #Extra #Noise"
    result = _apply_hashtag_policy(
        post_text=post,
        core_hashtags=("#AIEngineering", "#AIResearch", "#LLM"),
        secondary_hashtags=("#AIAgents", "#DeveloperTools", "#MachineLearning"),
    )

    hashtags = [token for token in result.split() if token.startswith("#")]
    assert hashtags[:3] == ["#AIEngineering", "#AIResearch", "#LLM"]
    assert len(hashtags) <= 5
