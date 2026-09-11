from __future__ import annotations

import pytest

from telegram_bot.summarizer import _parse_thread_payload


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
