#!/usr/bin/env python3
"""Summarization and thread generation via OpenAI or Azure OpenAI."""

from __future__ import annotations

import ast
import json
import logging
from typing import Any

from openai import AzureOpenAI, BadRequestError, OpenAI

from telegram_bot.config import LLMConfig
from telegram_bot.prompts import system_prompt_linkedin, system_prompt_x

logger = logging.getLogger(__name__)


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
            "max_tokens": 1200,
        }

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
                    request.pop("max_tokens", None)
                    request["max_completion_tokens"] = 1200
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

        content = response.choices[0].message.content
        if not content:
            raise ValueError("Model returned empty content.")
        return content

    def generate_linkedin_post(self, blog_text: str) -> str:
        answer = self._complete(
            system_prompt=system_prompt_linkedin,
            user_message=(
                "Here is the scraped blog post text:\n"
                f"{blog_text}\n\n"
                "Return only the final LinkedIn post text."
            ),
        )
        return answer.replace("*", "").strip()

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


def _parse_thread_payload(raw_payload: str) -> dict[int, str]:
    payload = raw_payload.strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        if payload.lower().startswith("python"):
            payload = payload[6:].strip()
        elif payload.lower().startswith("json"):
            payload = payload[4:].strip()

    data: Any
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = ast.literal_eval(payload)

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
