"""Bounded image generation and shared upload validation."""

from __future__ import annotations

import base64
import io
import warnings

from openai import AzureOpenAI, OpenAI
from PIL import Image

from telegram_bot.config import ImageConfig

MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_BASE64_LENGTH = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
_MAX_PROMPT_LENGTH = 4000


class ImageGenerationError(ValueError):
    """Safe to display to the user; never includes provider response details."""


def validate_image_bytes(data: bytes) -> None:
    """Accept only decoded, single-frame PNG/JPEG within conservative upload limits."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise ImageGenerationError("The image must be nonempty and no larger than 10 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                # These bounds also satisfy Telegram's dimension-sum and aspect-ratio limits.
                if (
                    image.format not in {"PNG", "JPEG"}
                    or getattr(image, "n_frames", 1) != 1
                    or not (64 <= width <= 4096 and 64 <= height <= 4096)
                    or max(width, height) > 20 * min(width, height)
                ):
                    raise ImageGenerationError("The image format or dimensions are unsupported.")
                image.verify()
            # verify() checks structure; a separate load catches corrupt/truncated pixel data.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except ImageGenerationError:
        raise
    except Exception:
        raise ImageGenerationError(
            "The image is not a valid PNG or JPEG. Please regenerate it."
        ) from None


class ImageGenerator:
    def __init__(self, config: ImageConfig):
        self._config = config

    def generate(self, prompt: str) -> bytes:
        if not self._config.enabled:
            raise ImageGenerationError("Image generation is disabled.")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= _MAX_PROMPT_LENGTH:
            raise ImageGenerationError("The image prompt must contain 1 to 4000 characters.")

        try:
            if self._config.provider == "azure_openai":
                client = AzureOpenAI(
                    api_key=self._config.api_key,
                    azure_endpoint=self._config.azure_endpoint,
                    api_version=self._config.api_version,
                    max_retries=0,
                    timeout=self._config.timeout_seconds,
                )
            else:
                client = OpenAI(
                    api_key=self._config.api_key,
                    base_url="https://api.openai.com/v1",
                    max_retries=0,
                    timeout=self._config.timeout_seconds,
                )
            with client:
                response = client.images.generate(
                    model=self._config.model,
                    prompt=prompt.strip(),
                    n=1,
                    size=self._config.size,
                    quality=self._config.quality,
                    output_format="png",
                )
            if not response.data or len(response.data) != 1:
                raise ImageGenerationError("The provider did not return one usable image.")
            encoded = response.data[0].b64_json
            if not isinstance(encoded, str) or len(encoded) > _MAX_BASE64_LENGTH:
                raise ImageGenerationError("The provider did not return a usable image.")
            data = base64.b64decode(encoded, validate=True)
            validate_image_bytes(data)
            with Image.open(io.BytesIO(data)) as image:
                with image.convert(
                    "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
                ) as normalized:
                    normalized.info.clear()
                    output = io.BytesIO()
                    normalized.save(output, format="PNG")
            png = output.getvalue()
            validate_image_bytes(png)
            return png
        except Exception:
            # A timeout can still be billable. The caller owns persistent per-draft attempt limits.
            raise ImageGenerationError(
                "Image generation did not produce a usable image. The attempt may still be charged; "
                "no automatic retry was made."
            ) from None
