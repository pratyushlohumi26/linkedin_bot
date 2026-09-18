from __future__ import annotations

import base64
import io
import json

import pytest
from openai import AzureOpenAI, OpenAI
from PIL import Image
from PIL.PngImagePlugin import PngInfo

try:
    import httpx2 as httpx
except ImportError:
    import httpx

from telegram_bot import image_generator as image_module
from telegram_bot.config import ImageConfig
from telegram_bot.image_generator import (
    ImageGenerationError,
    ImageGenerator,
    validate_image_bytes,
)


def image_bytes(fmt: str = "PNG", size: tuple[int, int] = (1024, 1024)) -> bytes:
    output = io.BytesIO()
    with Image.new("RGB", size, color="navy") as image:
        image.save(output, format=fmt)
    return output.getvalue()


def install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list:
    clients = []

    # Only HTTP is substituted: requests, SDK parsing and Pillow decoding remain real.
    def factory(cls):
        def build(**kwargs):
            client = cls(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
            clients.append(client)
            return client

        return build

    monkeypatch.setattr(image_module, "OpenAI", factory(OpenAI))
    monkeypatch.setattr(image_module, "AzureOpenAI", factory(AzureOpenAI))
    return clients


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_validate_real_images(fmt: str) -> None:
    assert validate_image_bytes(image_bytes(fmt)) is None


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an image",
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff",
        b"x" * (10 * 1024 * 1024 + 1),
        image_bytes("GIF"),
        image_bytes("WEBP"),
        image_bytes()[:100],
        image_bytes("JPEG")[:-100],
        image_bytes(size=(32, 32)),
        image_bytes(size=(4097, 100)),
        image_bytes(size=(4096, 64)),
    ],
    ids=[
        "empty",
        "garbage",
        "png-header",
        "jpeg-header",
        "oversized",
        "gif",
        "webp",
        "truncated-png",
        "truncated-jpeg",
        "tiny",
        "too-wide",
        "aspect-ratio",
    ],
)
def test_reject_invalid_images(data: bytes) -> None:
    with pytest.raises(ImageGenerationError, match="image"):
        validate_image_bytes(data)


def test_reject_animated_png() -> None:
    output = io.BytesIO()
    with (
        Image.new("RGB", (128, 128), "red") as first,
        Image.new("RGB", (128, 128), "blue") as second,
    ):
        first.save(output, format="PNG", save_all=True, append_images=[second])
    with pytest.raises(ImageGenerationError):
        validate_image_bytes(output.getvalue())


@pytest.mark.parametrize("provider", ["openai", "azure_openai"])
@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_generate_real_sdk_returns_valid_png(
    monkeypatch: pytest.MonkeyPatch, provider: str, fmt: str
) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "created": 1,
                "data": [{"b64_json": base64.b64encode(image_bytes(fmt)).decode()}],
            },
        )

    clients = install_transport(monkeypatch, handler)
    config = ImageConfig(
        enabled=True,
        provider=provider,
        api_key="test-image-key",
        model="image-deployment" if provider == "azure_openai" else "gpt-image-1-mini",
        azure_endpoint=(
            "https://example.openai.azure.com/openai/v1/" if provider == "azure_openai" else None
        ),
        timeout_seconds=42,
    )
    result = ImageGenerator(config).generate(
        "Editorial illustration of a compact toolbox, no text."
    )
    validate_image_bytes(result)
    with Image.open(io.BytesIO(result)) as image:
        assert image.format == "PNG"
        assert image.size == (1024, 1024)
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["n"] == 1
    assert payload["size"] == "1024x1024"
    assert payload["quality"] == "low"
    assert payload["model"] == config.model
    assert payload["output_format"] == "png"
    assert clients[0].max_retries == 0
    assert clients[0].timeout == 42
    assert clients[0].is_closed()
    if provider == "azure_openai":
        assert requests[0].url.host == "example.openai.azure.com"
        assert requests[0].url.path == "/openai/deployments/image-deployment/images/generations"
        assert requests[0].url.params["api-version"] == "2025-04-01-preview"
        assert requests[0].headers["api-key"] == "test-image-key"
    else:
        assert requests[0].url.host == "api.openai.com"
        assert requests[0].headers["authorization"] == "Bearer test-image-key"


def test_disabled_generator_never_calls_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request):
        pytest.fail("Disabled images must not call a provider")

    clients = install_transport(monkeypatch, handler)
    with pytest.raises(ImageGenerationError, match="disabled"):
        ImageGenerator(ImageConfig()).generate("A useful prompt")
    assert clients == []


@pytest.mark.parametrize("prompt", ["", " ", "x" * 4001, None])
def test_invalid_prompt_never_calls_provider(monkeypatch: pytest.MonkeyPatch, prompt: str) -> None:
    def handler(request):
        pytest.fail("Invalid prompts must not call a provider")

    clients = install_transport(monkeypatch, handler)
    with pytest.raises(ImageGenerationError, match="prompt"):
        ImageGenerator(ImageConfig(enabled=True, api_key="test-key")).generate(prompt)
    assert clients == []


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_provider_errors_safe_and_never_retried(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={"error": {"message": "secret-key-and-provider-detail", "type": "server_error"}},
        )

    clients = install_transport(monkeypatch, handler)
    with pytest.raises(ImageGenerationError) as exc:
        ImageGenerator(ImageConfig(enabled=True, api_key="test-key")).generate(
            "An editorial illustration"
        )
    assert "secret-key" not in str(exc.value)
    assert "provider-detail" not in str(exc.value)
    assert exc.value.__suppress_context__
    assert len(requests) == 1
    assert clients[0].is_closed()


def test_timeout_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("sensitive-detail", request=request)

    install_transport(monkeypatch, handler)
    with pytest.raises(ImageGenerationError) as exc:
        ImageGenerator(ImageConfig(enabled=True, api_key="test-key")).generate(
            "An editorial illustration"
        )
    assert "sensitive-detail" not in str(exc.value)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{}],
        [{"url": "https://untrusted.example/image.png"}],
        [{"b64_json": "not valid base64!"}],
        [{"b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()}],
        [{"b64_json": "A" * (14 * 1024 * 1024)}],
        [{"b64_json": "abcd"}, {"b64_json": "abcd"}],
    ],
)
def test_invalid_provider_images_rejected_without_url_fetch(
    monkeypatch: pytest.MonkeyPatch, data: list
) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"created": 1, "data": data})

    install_transport(monkeypatch, handler)
    with pytest.raises(ImageGenerationError):
        ImageGenerator(ImageConfig(enabled=True, api_key="test-key")).generate(
            "An editorial illustration"
        )
    assert len(requests) == 1


def test_generator_strips_image_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    output = io.BytesIO()
    metadata = PngInfo()
    metadata.add_text("Comment", "provider-private-metadata")
    with Image.new("RGBA", (128, 128), (20, 30, 40, 127)) as image:
        image.save(output, format="PNG", pnginfo=metadata)

    def handler(request):
        return httpx.Response(
            200,
            json={
                "created": 1,
                "data": [{"b64_json": base64.b64encode(output.getvalue()).decode()}],
            },
        )

    install_transport(monkeypatch, handler)
    data = ImageGenerator(ImageConfig(enabled=True, api_key="test-key")).generate(
        "An editorial image"
    )
    assert b"provider-private-metadata" not in data
    with Image.open(io.BytesIO(data)) as image:
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0)) == (20, 30, 40, 127)
