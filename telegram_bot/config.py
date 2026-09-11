#!/usr/bin/env python3
"""Environment-backed configuration for the Telegram social bot."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal

from dotenv import load_dotenv

LlmProvider = Literal["openai", "azure_openai"]
TelegramMode = Literal["polling", "webhook"]

_DEFAULT_CORE_HASHTAGS = ("#AIEngineering", "#AIResearch", "#LLM")
_DEFAULT_SECONDARY_HASHTAGS = (
    "#AIAgents",
    "#MachineLearning",
    "#GenerativeAI",
    "#DeveloperTools",
)


@dataclass(frozen=True)
class LLMConfig:
    provider: LlmProvider
    model: str
    openai_api_key: str | None = None
    azure_openai_api_key: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_version: str = "2024-06-01"


@dataclass(frozen=True)
class XCredentials:
    api_key: str | None
    api_secret_key: str | None
    access_token: str | None
    access_token_secret: str | None

    @property
    def is_configured(self) -> bool:
        return all(
            [
                self.api_key,
                self.api_secret_key,
                self.access_token,
                self.access_token_secret,
            ]
        )


@dataclass(frozen=True)
class AppConfig:
    telegram_token: str
    telegram_mode: TelegramMode
    telegram_drop_pending_updates: bool
    telegram_webhook_listen: str
    telegram_webhook_port: int
    telegram_webhook_path: str
    telegram_webhook_public_url: str | None
    telegram_webhook_secret_token: str | None
    linkedin_token: str | None
    linkedin_enable_first_comment: bool
    linkedin_first_comment_delay_seconds: int
    linkedin_hashtag_core: tuple[str, ...]
    linkedin_hashtag_secondary: tuple[str, ...]
    enable_research_agent: bool
    search_provider: str
    search_api_key: str | None
    search_max_links: int
    telemetry_log_path: str
    x_credentials: XCredentials
    llm: LLMConfig
    scraper_timeout_seconds: int
    allowed_user_ids: set[int]


def _get_env(name: str, *, required: bool = False, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is not None:
        value = value.strip()
    if required and not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value or None


def _parse_bool(raw_value: str | None, *, default: bool) -> bool:
    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    raise ValueError(f"Invalid boolean value: {raw_value!r}")


def _parse_allowed_user_ids(raw_value: str | None) -> set[int]:
    if not raw_value:
        return set()

    user_ids: set[int] = set()
    for chunk in raw_value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        user_ids.add(int(chunk))
    return user_ids


def _parse_csv(raw_value: str | None) -> tuple[str, ...]:
    if not raw_value:
        return ()
    return tuple(item.strip() for item in raw_value.split(",") if item.strip())


def _parse_int(raw_value: str | None, *, default: int, env_name: str) -> int:
    if raw_value is None:
        return default

    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{env_name} must be an integer.") from exc


def _normalize_webhook_path(path_value: str | None) -> str:
    path = (path_value or "webhook").strip().strip("/")
    return path or "webhook"


def _normalize_azure_endpoint(endpoint: str) -> str:
    normalized = endpoint.strip().rstrip("/")
    for suffix in ("/openai/v1", "/openai"):
        if normalized.lower().endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized


def _normalize_hashtag(tag: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", tag.lstrip("#"))
    if not cleaned:
        raise ValueError(f"Invalid hashtag value: {tag!r}")
    return f"#{cleaned}"


def _parse_hashtag_list(raw_value: str | None, *, default: tuple[str, ...]) -> tuple[str, ...]:
    values = _parse_csv(raw_value)
    if not values:
        values = default

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        hashtag = _normalize_hashtag(value)
        key = hashtag.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(hashtag)
    return tuple(normalized)


def load_config() -> AppConfig:
    load_dotenv()

    telegram_token = _get_env("TELEGRAM_TOKEN", required=True)
    telegram_mode = (_get_env("TELEGRAM_MODE", default="polling") or "polling").lower()
    if telegram_mode not in {"polling", "webhook"}:
        raise ValueError("TELEGRAM_MODE must be either 'polling' or 'webhook'.")

    provider = (_get_env("LLM_PROVIDER", default="openai") or "openai").lower()
    if provider not in {"openai", "azure_openai"}:
        raise ValueError("LLM_PROVIDER must be either 'openai' or 'azure_openai'.")

    if provider == "azure_openai":
        llm = LLMConfig(
            provider="azure_openai",
            model=_get_env("AZURE_OPENAI_DEPLOYMENT", required=True) or "",
            azure_openai_api_key=_get_env("AZURE_OPENAI_API_KEY", required=True),
            azure_openai_endpoint=_normalize_azure_endpoint(
                _get_env("AZURE_OPENAI_ENDPOINT", required=True) or ""
            ),
            azure_openai_api_version=(
                _get_env("AZURE_OPENAI_API_VERSION", default="2024-06-01") or "2024-06-01"
            ),
        )
    else:
        llm = LLMConfig(
            provider="openai",
            model=_get_env("OPENAI_MODEL", default="gpt-4.1") or "gpt-4.1",
            openai_api_key=_get_env("OPENAI_API_KEY", required=True),
        )

    telegram_webhook_public_url = _get_env("TELEGRAM_WEBHOOK_PUBLIC_URL")
    if telegram_mode == "webhook" and not telegram_webhook_public_url:
        raise ValueError("TELEGRAM_WEBHOOK_PUBLIC_URL is required when TELEGRAM_MODE=webhook.")

    scraper_timeout = _parse_int(
        _get_env("SCRAPER_TIMEOUT_SECONDS", default="10"),
        default=10,
        env_name="SCRAPER_TIMEOUT_SECONDS",
    )

    search_max_links = max(
        1,
        min(
            _parse_int(
                _get_env("SEARCH_MAX_LINKS", default="3"),
                default=3,
                env_name="SEARCH_MAX_LINKS",
            ),
            5,
        ),
    )

    return AppConfig(
        telegram_token=telegram_token or "",
        telegram_mode=telegram_mode,  # type: ignore[arg-type]
        telegram_drop_pending_updates=_parse_bool(
            _get_env("TELEGRAM_DROP_PENDING_UPDATES"), default=True
        ),
        telegram_webhook_listen=_get_env("TELEGRAM_WEBHOOK_LISTEN", default="0.0.0.0") or "0.0.0.0",
        telegram_webhook_port=_parse_int(
            _get_env("TELEGRAM_WEBHOOK_PORT", default="8443"),
            default=8443,
            env_name="TELEGRAM_WEBHOOK_PORT",
        ),
        telegram_webhook_path=_normalize_webhook_path(_get_env("TELEGRAM_WEBHOOK_PATH")),
        telegram_webhook_public_url=(
            telegram_webhook_public_url.rstrip("/") if telegram_webhook_public_url else None
        ),
        telegram_webhook_secret_token=_get_env("TELEGRAM_WEBHOOK_SECRET_TOKEN"),
        linkedin_token=_get_env("LINKEDIN_TOKEN"),
        linkedin_enable_first_comment=_parse_bool(
            _get_env("LINKEDIN_ENABLE_FIRST_COMMENT"),
            default=False,
        ),
        linkedin_first_comment_delay_seconds=max(
            0,
            _parse_int(
                _get_env("LINKEDIN_FIRST_COMMENT_DELAY_SECONDS", default="90"),
                default=90,
                env_name="LINKEDIN_FIRST_COMMENT_DELAY_SECONDS",
            ),
        ),
        linkedin_hashtag_core=_parse_hashtag_list(
            _get_env("LINKEDIN_HASHTAG_CORE"),
            default=_DEFAULT_CORE_HASHTAGS,
        ),
        linkedin_hashtag_secondary=_parse_hashtag_list(
            _get_env("LINKEDIN_HASHTAG_SECONDARY"),
            default=_DEFAULT_SECONDARY_HASHTAGS,
        ),
        enable_research_agent=_parse_bool(_get_env("ENABLE_RESEARCH_AGENT"), default=False),
        search_provider=_get_env("SEARCH_PROVIDER", default="tavily") or "tavily",
        search_api_key=_get_env("SEARCH_API_KEY"),
        search_max_links=search_max_links,
        telemetry_log_path=(
            _get_env("PIPELINE_TELEMETRY_PATH", default=".runtime/telemetry.jsonl")
            or ".runtime/telemetry.jsonl"
        ),
        x_credentials=XCredentials(
            api_key=_get_env("X_API_KEY"),
            api_secret_key=_get_env("X_API_SECRET_KEY"),
            access_token=_get_env("X_ACCESS_TOKEN"),
            access_token_secret=_get_env("X_ACCESS_TOKEN_SECRET"),
        ),
        llm=llm,
        scraper_timeout_seconds=scraper_timeout,
        allowed_user_ids=_parse_allowed_user_ids(_get_env("TELEGRAM_ALLOWED_USER_IDS")),
    )
