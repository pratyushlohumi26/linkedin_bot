from __future__ import annotations

import pytest

from telegram_bot.config import load_config

ENV_KEYS = [
    "TELEGRAM_TOKEN",
    "TELEGRAM_MODE",
    "TELEGRAM_DROP_PENDING_UPDATES",
    "TELEGRAM_WEBHOOK_PUBLIC_URL",
    "TELEGRAM_WEBHOOK_PATH",
    "TELEGRAM_WEBHOOK_LISTEN",
    "TELEGRAM_WEBHOOK_PORT",
    "TELEGRAM_WEBHOOK_SECRET_TOKEN",
    "TELEGRAM_ALLOWED_USER_IDS",
    "SCRAPER_TIMEOUT_SECONDS",
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_DEPLOYMENT",
    "LINKEDIN_TOKEN",
    "X_API_KEY",
    "X_API_SECRET_KEY",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
]


def _clear_bot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_load_config_openai_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_bot_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:token")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    config = load_config()

    assert config.telegram_mode == "polling"
    assert config.telegram_drop_pending_updates is True
    assert config.telegram_webhook_path == "webhook"
    assert config.llm.provider == "openai"
    assert config.llm.model == "gpt-4.1"


def test_load_config_azure_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_bot_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:token")
    monkeypatch.setenv("LLM_PROVIDER", "azure_openai")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt4o-prod")

    config = load_config()

    assert config.llm.provider == "azure_openai"
    assert config.llm.model == "gpt4o-prod"
    assert config.llm.azure_openai_endpoint == "https://example.openai.azure.com"


def test_webhook_mode_requires_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_bot_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:token")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")

    with pytest.raises(ValueError, match="TELEGRAM_WEBHOOK_PUBLIC_URL"):
        load_config()


def test_webhook_path_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_bot_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:token")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_PUBLIC_URL", "https://bot.example.com/")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_PATH", "/telegram/events/")

    config = load_config()

    assert config.telegram_webhook_path == "telegram/events"
    assert config.telegram_webhook_public_url == "https://bot.example.com"


def test_allowed_users_and_drop_pending_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_bot_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:token")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1001, 1002")
    monkeypatch.setenv("TELEGRAM_DROP_PENDING_UPDATES", "false")

    config = load_config()

    assert config.allowed_user_ids == {1001, 1002}
    assert config.telegram_drop_pending_updates is False
