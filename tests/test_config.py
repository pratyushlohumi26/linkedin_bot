from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest
from dotenv import load_dotenv

from telegram_bot import config as config_module
from telegram_bot.config import ImageConfig, load_config

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
    "LINKEDIN_ENABLE_FIRST_COMMENT",
    "LINKEDIN_FIRST_COMMENT_DELAY_SECONDS",
    "LINKEDIN_HASHTAG_CORE",
    "LINKEDIN_HASHTAG_SECONDARY",
    "ENABLE_RESEARCH_AGENT",
    "SEARCH_PROVIDER",
    "SEARCH_API_KEY",
    "SEARCH_MAX_LINKS",
    "PIPELINE_TELEMETRY_PATH",
    "X_API_KEY",
    "X_API_SECRET_KEY",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
    "LINKEDIN_ENABLE_IMAGES",
    "IMAGE_PROVIDER",
    "OPENAI_IMAGE_MODEL",
    "IMAGE_SIZE",
    "IMAGE_QUALITY",
    "IMAGE_MAX_GENERATIONS",
    "IMAGE_TIMEOUT_SECONDS",
    "AZURE_OPENAI_IMAGE_DEPLOYMENT",
    "AZURE_OPENAI_IMAGE_API_VERSION",
    "DRAFT_STORE_PATH",
    "DRAFT_RETENTION_DAYS",
    "PYTHON_DOTENV_DISABLED",
]


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Redirect only the filesystem boundary; exercise real dotenv and config parsing.
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("")
    monkeypatch.setattr(config_module, "load_dotenv", partial(load_dotenv, dotenv_path=dotenv_path))
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_load_config_openai_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:token")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4.1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    config = load_config()

    assert config.telegram_mode == "polling"
    assert config.telegram_drop_pending_updates is True
    assert config.telegram_webhook_path == "webhook"
    assert config.llm.provider == "openai"
    assert config.llm.model == "gpt-4.1"
    assert config.linkedin_enable_first_comment is False
    assert config.linkedin_hashtag_core == ("#AIEngineering", "#AIResearch", "#LLM")
    assert config.search_provider == "tavily"


def test_load_config_azure_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:token")
    monkeypatch.setenv("LLM_PROVIDER", "azure_openai")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt4o-prod")

    config = load_config()

    assert config.llm.provider == "azure_openai"
    assert config.llm.model == "gpt4o-prod"
    assert config.llm.azure_openai_endpoint == "https://example.openai.azure.com"


def test_load_config_azure_endpoint_suffix_is_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:token")
    monkeypatch.setenv("LLM_PROVIDER", "azure_openai")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.services.ai.azure.com/openai/v1")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt4o-prod")

    config = load_config()

    assert config.llm.azure_openai_endpoint == "https://example.services.ai.azure.com"


def test_webhook_mode_requires_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:token")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")

    with pytest.raises(ValueError, match="TELEGRAM_WEBHOOK_PUBLIC_URL"):
        load_config()


def test_webhook_path_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:token")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_PUBLIC_URL", "https://bot.example.com/")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_PATH", "/telegram/events/")

    config = load_config()

    assert config.telegram_webhook_path == "telegram/events"
    assert config.telegram_webhook_public_url == "https://bot.example.com"


def test_allowed_users_drop_pending_and_hashtag_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:token")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1001, 1002")
    monkeypatch.setenv("TELEGRAM_DROP_PENDING_UPDATES", "false")
    monkeypatch.setenv("LINKEDIN_ENABLE_FIRST_COMMENT", "true")
    monkeypatch.setenv("LINKEDIN_FIRST_COMMENT_DELAY_SECONDS", "12")
    monkeypatch.setenv("LINKEDIN_HASHTAG_CORE", "ai engineering,#AIResearch, #AIResearch")
    monkeypatch.setenv("SEARCH_MAX_LINKS", "10")

    config = load_config()

    assert config.allowed_user_ids == {1001, 1002}
    assert config.telegram_drop_pending_updates is False
    assert config.linkedin_enable_first_comment is True
    assert config.linkedin_first_comment_delay_seconds == 12
    assert config.linkedin_hashtag_core == ("#aiengineering", "#AIResearch")
    assert config.search_max_links == 5


def _set_text_config(monkeypatch: pytest.MonkeyPatch, *, azure: bool = False) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:token")
    if azure:
        monkeypatch.setenv("LLM_PROVIDER", "azure_openai")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-azure-key")
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/openai/v1/")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "text-deployment")
    else:
        monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")


def test_image_defaults_are_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_text_config(monkeypatch, azure=True)
    config = load_config()
    assert config.images == ImageConfig()
    assert config.draft_store_path == ".runtime/drafts.sqlite3"
    assert config.draft_retention_days == 7


def test_openai_image_credentials_independent_of_azure_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_text_config(monkeypatch, azure=True)
    monkeypatch.setenv("LINKEDIN_ENABLE_IMAGES", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "independent-image-key")
    monkeypatch.setenv("OPENAI_IMAGE_MODEL", "gpt-image-1-mini")
    config = load_config()
    assert config.llm.openai_api_key is None
    assert config.images.api_key == "independent-image-key"
    assert config.images.enabled is True
    assert "independent-image-key" not in repr(config.images)


def test_azure_images_with_openai_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_text_config(monkeypatch)
    monkeypatch.setenv("LINKEDIN_ENABLE_IMAGES", "true")
    monkeypatch.setenv("IMAGE_PROVIDER", "azure_openai")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-azure-image-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/openai/")
    monkeypatch.setenv("AZURE_OPENAI_IMAGE_DEPLOYMENT", "image-deployment")
    monkeypatch.setenv("AZURE_OPENAI_IMAGE_API_VERSION", "2025-04-01-preview")
    config = load_config()
    assert config.images.provider == "azure_openai"
    assert config.images.model == "image-deployment"
    assert config.images.api_key == "test-azure-image-key"
    assert config.images.azure_endpoint == "https://example.openai.azure.com"
    assert config.images.api_version == "2025-04-01-preview"


@pytest.mark.parametrize(
    "missing", ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_IMAGE_DEPLOYMENT"]
)
def test_enabled_azure_images_require_settings(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _set_text_config(monkeypatch)
    monkeypatch.setenv("IMAGE_PROVIDER", "azure_openai")
    monkeypatch.setenv("LINKEDIN_ENABLE_IMAGES", "true")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_IMAGE_DEPLOYMENT", "image-deployment")
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match=missing):
        load_config()


def test_disabled_azure_images_need_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_text_config(monkeypatch)
    monkeypatch.setenv("IMAGE_PROVIDER", "azure_openai")
    assert load_config().images.api_key is None


def test_enabled_openai_images_require_independent_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_text_config(monkeypatch, azure=True)
    monkeypatch.setenv("LINKEDIN_ENABLE_IMAGES", "true")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        load_config()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("IMAGE_PROVIDER", "unsupported"),
        ("IMAGE_SIZE", "auto"),
        ("IMAGE_QUALITY", "very-expensive"),
        ("IMAGE_MAX_GENERATIONS", "0"),
        ("IMAGE_MAX_GENERATIONS", "11"),
        ("IMAGE_MAX_GENERATIONS", "many"),
        ("IMAGE_TIMEOUT_SECONDS", "0"),
        ("IMAGE_TIMEOUT_SECONDS", "601"),
        ("DRAFT_RETENTION_DAYS", "0"),
        ("DRAFT_RETENTION_DAYS", "366"),
    ],
)
def test_image_and_draft_config_reject_invalid_bounds(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    _set_text_config(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        load_config()


def test_image_settings_and_real_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_text_config(monkeypatch)
    (tmp_path / ".env").write_text(
        "IMAGE_SIZE=1536x1024\nIMAGE_QUALITY=medium\nIMAGE_MAX_GENERATIONS=5\n"
        "IMAGE_TIMEOUT_SECONDS=60\nDRAFT_STORE_PATH=.runtime/test.sqlite3\nDRAFT_RETENTION_DAYS=14\n"
    )
    config = load_config()
    assert config.images.size == "1536x1024"
    assert config.images.quality == "medium"
    assert config.images.max_generations == 5
    assert config.images.timeout_seconds == 60
    assert config.draft_store_path == ".runtime/test.sqlite3"
    assert config.draft_retention_days == 14


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.openai.azure.com",
        "https://user:secret@example.openai.azure.com",
        "https://example.openai.azure.com/?key=secret",
        "https://example.openai.azure.com/deployments/x",
        "not-a-url",
        "https://[invalid",
    ],
)
def test_azure_image_endpoint_validation_is_safe(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    _set_text_config(monkeypatch)
    monkeypatch.setenv("IMAGE_PROVIDER", "azure_openai")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT") as exc:
        load_config()
    assert endpoint not in str(exc.value)


@pytest.mark.parametrize(("field", "value"), [("max_generations", True), ("timeout_seconds", 1.5)])
def test_direct_image_config_rejects_non_integer_limits(field: str, value) -> None:
    with pytest.raises(ValueError):
        ImageConfig(**{field: value})
