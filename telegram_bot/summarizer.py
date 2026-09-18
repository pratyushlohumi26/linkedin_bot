#!/usr/bin/env python3
"""Summarization and thread generation via OpenAI or Azure OpenAI."""

from __future__ import annotations

import ast
import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from openai import AzureOpenAI, BadRequestError, OpenAI

from telegram_bot.config import LLMConfig
from telegram_bot.prompts import (
    build_image_brief_user_prompt,
    build_linkedin_first_comment_user_prompt,
    build_linkedin_variant_user_prompt,
    system_prompt_image_brief,
    system_prompt_linkedin_first_comment,
    system_prompt_linkedin_variants,
    system_prompt_x,
)

logger = logging.getLogger(__name__)
_HASHTAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_]+)")
_DEFAULT_COMPLETION_TOKEN_BUDGET = 1800
_MAX_COMPLETION_TOKEN_BUDGET = 3200


class ContentGenerator:
    """Generates LinkedIn posts and X threads from scraped blog text."""

    def __init__(self, llm_config: LLMConfig):
        self._llm_config = llm_config
        if llm_config.provider == "azure_openai":
            self._client = AzureOpenAI(
                api_key=llm_config.azure_openai_api_key,
                api_version=llm_config.azure_openai_api_version,
                azure_endpoint=llm_config.azure_openai_endpoint,
            )
        else:
            self._client = OpenAI(api_key=llm_config.openai_api_key)

    def _complete(self, *, system_prompt: str, user_message: str) -> str:
        request = {
            "model": self._llm_config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.7,
            "max_tokens": _DEFAULT_COMPLETION_TOKEN_BUDGET,
        }

        for attempt in range(1, 5):
            while True:
                try:
                    response = self._client.chat.completions.create(**request)
                    break
                except BadRequestError as err:
                    err_text = str(err)

                    if (
                        "max_tokens" in request
                        and "max_tokens" in err_text
                        and "max_completion_tokens" in err_text
                    ):
                        logger.info(
                            "Retrying completion with max_completion_tokens for model %s",
                            self._llm_config.model,
                        )
                        budget = int(request.pop("max_tokens", _DEFAULT_COMPLETION_TOKEN_BUDGET))
                        request["max_completion_tokens"] = budget
                        continue

                    if (
                        "temperature" in request
                        and "temperature" in err_text
                        and "default (1) value is supported" in err_text
                    ):
                        logger.info(
                            "Retrying completion without explicit temperature for model %s",
                            self._llm_config.model,
                        )
                        request.pop("temperature", None)
                        continue

                    raise

            content = _extract_content_text(response.choices[0].message)
            if content:
                return content

            refusal = getattr(response.choices[0].message, "refusal", None)
            if refusal:
                raise ValueError(f"Model refused request: {refusal}")

            finish_reason = getattr(response.choices[0], "finish_reason", None)
            if finish_reason == "length" and _increase_completion_budget(request):
                logger.warning(
                    "Model hit length limit with empty content (attempt %s/4); increased token budget and retrying.",
                    attempt,
                )
                continue

            logger.warning(
                "Model returned empty content (attempt %s/4); retrying.",
                attempt,
            )

        raise ValueError("Model returned empty content after retries.")

    def generate_linkedin_variants(
        self,
        blog_text: str,
        *,
        core_hashtags: Sequence[str],
        secondary_hashtags: Sequence[str],
    ) -> dict[str, str]:
        answer = self._complete(
            system_prompt=system_prompt_linkedin_variants,
            user_message=build_linkedin_variant_user_prompt(
                blog_text=blog_text,
                core_hashtags=core_hashtags,
                secondary_hashtags=secondary_hashtags,
            ),
        )
        variants = _parse_linkedin_variants(answer)
        return {
            key: _apply_hashtag_policy(
                post_text=value,
                core_hashtags=core_hashtags,
                secondary_hashtags=secondary_hashtags,
            )
            for key, value in variants.items()
        }

    def generate_linkedin_post(self, blog_text: str) -> str:
        variants = self.generate_linkedin_variants(
            blog_text,
            core_hashtags=("#AIEngineering", "#AIResearch", "#LLM"),
            secondary_hashtags=("#AIAgents", "#DeveloperTools", "#MachineLearning"),
        )
        return variants["B"]

    def generate_linkedin_first_comment(
        self,
        *,
        linkedin_post: str,
        article_excerpt: str,
        references: Sequence[dict[str, str]],
    ) -> str:
        answer = self._complete(
            system_prompt=system_prompt_linkedin_first_comment,
            user_message=build_linkedin_first_comment_user_prompt(
                linkedin_post=linkedin_post,
                article_excerpt=article_excerpt,
                references=references,
            ),
        )
        return answer.replace("*", "").strip()

    def generate_image_brief(
        self, article_text: str, post_text: str, *, instructions: str = ""
    ) -> dict[str, Any]:
        if not isinstance(article_text, str) or not article_text.strip():
            raise ValueError("An article is required for an image brief.")
        if not isinstance(post_text, str) or not post_text.strip():
            raise ValueError("A selected draft is required for an image brief.")
        if not isinstance(instructions, str):
            raise ValueError("Image brief instructions must be text.")
        try:
            answer = self._complete(
                system_prompt=system_prompt_image_brief,
                user_message=build_image_brief_user_prompt(
                    article_text=article_text, post_text=post_text, instructions=instructions
                ),
            )
            return _parse_image_brief(answer)
        except Exception:
            raise ValueError("Could not generate a valid image brief. Please try again.") from None

    def generate_x_thread(self, blog_text: str) -> dict[int, str]:
        answer = self._complete(
            system_prompt=system_prompt_x,
            user_message=(
                "Here is the scraped blog post text:\n"
                f"{blog_text}\n\n"
                "Return only a dict-like or JSON object mapping tweet positions to tweet text."
            ),
        )
        return _parse_thread_payload(answer)


def _parse_image_brief(raw_payload: str) -> dict[str, Any]:
    error = "Invalid image brief. Please regenerate the brief."
    if not isinstance(raw_payload, str) or len(raw_payload) > 16000:
        raise ValueError(error)
    try:
        data = json.loads(raw_payload)
    except (ValueError, RecursionError):
        raise ValueError(error) from None
    bounds = {
        "message": (10, 300),
        "concept": (10, 600),
        "prompt": (40, 3000),
        "alt_text": (10, 1000),
    }
    if (
        not isinstance(data, dict)
        or not set(bounds) <= data.keys()
        or data.keys() - (set(bounds) | {"source_facts"})
    ):
        raise ValueError(error)

    def clean(value: Any, minimum: int, maximum: int) -> str:
        if not isinstance(value, str):
            raise ValueError(error)
        value = value.strip()
        if not minimum <= len(value) <= maximum or any(
            (ord(char) < 32 and char not in "\n\t")
            or 0x7F <= ord(char) <= 0x9F
            or 0xD800 <= ord(char) <= 0xDFFF
            for char in value
        ):
            raise ValueError(error)
        return value

    result: dict[str, Any] = {key: clean(data[key], *limit) for key, limit in bounds.items()}
    if "source_facts" in data:
        facts = data["source_facts"]
        if not isinstance(facts, list) or len(facts) > 6:
            raise ValueError(error)
        result["source_facts"] = [clean(fact, 1, 300) for fact in facts]
    return result


def _normalize_hashtag(tag: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", tag.lstrip("#"))
    if not cleaned:
        raise ValueError(f"Invalid hashtag value: {tag!r}")
    return f"#{cleaned}"


def _increase_completion_budget(request: dict[str, Any]) -> bool:
    token_field = "max_completion_tokens" if "max_completion_tokens" in request else "max_tokens"

    current_budget = int(request.get(token_field, _DEFAULT_COMPLETION_TOKEN_BUDGET))
    if current_budget >= _MAX_COMPLETION_TOKEN_BUDGET:
        return False

    request[token_field] = min(current_budget + 600, _MAX_COMPLETION_TOKEN_BUDGET)
    return True


def _extract_content_text(message: Any) -> str | None:
    raw_content = getattr(message, "content", None)
    if isinstance(raw_content, str):
        text = raw_content.strip()
        return text or None

    if isinstance(raw_content, list):
        parts: list[str] = []
        for item in raw_content:
            if isinstance(item, dict):
                text = item.get("text")
            else:
                text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        if parts:
            return "\n".join(parts)

    return None


def _parse_json_like(raw_payload: str) -> Any:
    payload = raw_payload.strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        if payload.lower().startswith("python"):
            payload = payload[6:].strip()
        elif payload.lower().startswith("json"):
            payload = payload[4:].strip()

    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return ast.literal_eval(payload)


def _parse_linkedin_variants(raw_payload: str) -> dict[str, str]:
    data = _parse_json_like(raw_payload)
    if not isinstance(data, dict):
        raise ValueError("LinkedIn variants payload must be a dictionary-like object.")

    parsed: dict[str, str] = {}
    for key in ("A", "B", "C"):
        value = data.get(key) or data.get(key.lower())
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing or invalid LinkedIn variant for key {key}.")
        parsed[key] = value.replace("*", "").strip()

    return parsed


def _curate_hashtags(
    post_text: str,
    *,
    core_hashtags: Sequence[str],
    secondary_hashtags: Sequence[str],
) -> list[str]:
    chosen: list[str] = []
    seen: set[str] = set()

    def add_tag(tag: str) -> None:
        normalized = _normalize_hashtag(tag)
        key = normalized.lower()
        if key in seen:
            return
        seen.add(key)
        chosen.append(normalized)

    for tag in core_hashtags:
        add_tag(tag)

    normalized_secondary = {
        _normalize_hashtag(tag).lower(): _normalize_hashtag(tag) for tag in secondary_hashtags
    }

    for match in _HASHTAG_RE.findall(post_text):
        candidate = f"#{match}"
        normalized = _normalize_hashtag(candidate)
        secondary = normalized_secondary.get(normalized.lower())
        if secondary:
            add_tag(secondary)
        if len(chosen) >= 5:
            return chosen[:5]

    for tag in secondary_hashtags:
        if len(chosen) >= 5:
            break
        add_tag(tag)

    return chosen[:5]


def _apply_hashtag_policy(
    *,
    post_text: str,
    core_hashtags: Sequence[str],
    secondary_hashtags: Sequence[str],
) -> str:
    hashtags = _curate_hashtags(
        post_text,
        core_hashtags=core_hashtags,
        secondary_hashtags=secondary_hashtags,
    )

    body = _HASHTAG_RE.sub("", post_text)
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    if not hashtags:
        return body

    return f"{body}\n\n{' '.join(hashtags)}"


def _parse_thread_payload(raw_payload: str) -> dict[int, str]:
    data = _parse_json_like(raw_payload)

    if not isinstance(data, dict):
        raise ValueError("Thread payload must be a dictionary-like object.")

    normalized: dict[int, str] = {}
    for key, value in data.items():
        try:
            index = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid thread index: {key!r}") from exc
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Invalid tweet content for index {index}.")
        normalized[index] = value.strip()

    if not normalized:
        raise ValueError("Generated thread is empty.")

    return dict(sorted(normalized.items(), key=lambda item: item[0]))
