#!/usr/bin/env python3
"""LinkedIn API helper for publishing generated content."""

from __future__ import annotations

import logging
import re
import struct
import time
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import requests

try:
    from PIL import Image
except ImportError:
    Image = None

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 36_000_000
MAX_IMAGE_DIMENSION = 8192
ASSET_POLL_ATTEMPTS = 5
ASSET_POLL_INTERVAL = 1.0
IMAGE_RECIPE = "urn:li:digitalmediaRecipe:feedshare-image"


class LinkedInUploadError(RuntimeError):
    """Image validation, upload, or readiness failed; no public post was attempted."""


class LinkedInPublishUncertain(RuntimeError):
    """Post creation may have succeeded. Do not automatically retry publication."""


def _read_image(image_path: str | Path) -> tuple[bytes, str]:
    if Image is None:
        raise LinkedInUploadError("Pillow is required to validate LinkedIn images.")
    try:
        with Path(image_path).open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
    except (OSError, ValueError):
        raise LinkedInUploadError("Unable to read the approved image.") from None
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise LinkedInUploadError("Image is empty or exceeds the upload size limit.")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=["PNG", "JPEG"]) as image:
                image_format = image.format
                width, height = image.size
                if (
                    min(width, height) < 1
                    or max(width, height) > MAX_IMAGE_DIMENSION
                    or width * height > MAX_IMAGE_PIXELS
                ):
                    raise LinkedInUploadError("Image dimensions exceed the upload limits.")
                if getattr(image, "is_animated", False):
                    raise LinkedInUploadError("Only static PNG or JPEG images are supported.")
                image.verify()
            with Image.open(BytesIO(data), formats=["PNG", "JPEG"]) as image:
                image.load()
    except (
        OSError,
        ValueError,
        SyntaxError,
        struct.error,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise LinkedInUploadError("Image must be a valid, complete PNG or JPEG.") from None
    return data, "image/png" if image_format == "PNG" else "image/jpeg"


def _image_asset_id(asset_urn: str) -> str:
    if not isinstance(asset_urn, str) or not re.fullmatch(
        r"urn:li:digitalmediaAsset:[A-Za-z0-9_-]+", asset_urn
    ):
        raise LinkedInUploadError("LinkedIn returned an invalid image asset identifier.")
    return asset_urn.rsplit(":", 1)[1]


def _validate_upload_url(url: str) -> None:
    try:
        if (
            not isinstance(url, str)
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or "\\" in url
        ):
            raise ValueError
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
            or not re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*linkedin\.com",
                parsed.hostname or "",
            )
        ):
            raise ValueError
    except ValueError:
        raise LinkedInUploadError(
            "LinkedIn returned an untrusted image upload destination."
        ) from None


@dataclass(frozen=True)
class LinkedInPublishResult:
    status_code: int
    post_urn: str | None


def build_linkedin_post_url(post_urn: str | None) -> str | None:
    if not post_urn:
        return None

    normalized = post_urn.strip()
    if not normalized.startswith("urn:li:"):
        return None

    return f"https://www.linkedin.com/feed/update/{normalized}/"


class LinkedinAutomate:
    """Small LinkedIn publisher wrapper."""

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "X-Restli-Protocol-Version": "2.0.0",
            }
        )

    def _get_user_id(self) -> str | None:
        url = "https://api.linkedin.com/v2/userinfo"
        try:
            resp = self.session.get(url, timeout=15, allow_redirects=False)
            if resp.status_code != 200:
                logger.error("LinkedIn identity lookup failed (HTTP %s).", resp.status_code)
                return None
            payload = resp.json()
            user_id = payload.get("sub") if isinstance(payload, dict) else None
            if isinstance(user_id, str) and user_id.strip():
                return user_id
            logger.error("LinkedIn identity lookup returned an invalid member identifier.")
        except (requests.RequestException, ValueError):
            logger.error("LinkedIn identity lookup failed.")
        return None

    def _upload_request(self, method: str, url: str, *, stage: str, **kwargs) -> requests.Response:
        try:
            response = self.session.request(
                method, url, timeout=30, allow_redirects=False, **kwargs
            )
        except requests.RequestException:
            raise LinkedInUploadError(f"LinkedIn image {stage} request failed.") from None
        if not 200 <= response.status_code < 300:
            raise LinkedInUploadError(
                f"LinkedIn image {stage} failed (HTTP {response.status_code})."
            )
        return response

    def _wait_for_image(self, asset_id: str) -> None:
        for attempt in range(ASSET_POLL_ATTEMPTS):
            response = self._upload_request(
                "GET", f"https://api.linkedin.com/v2/assets/{asset_id}", stage="readiness check"
            )
            try:
                payload = response.json()
            except ValueError:
                raise LinkedInUploadError(
                    "LinkedIn returned invalid image readiness data."
                ) from None
            if not isinstance(payload, dict) or not isinstance(payload.get("recipes"), list):
                raise LinkedInUploadError("LinkedIn returned invalid image readiness data.")
            if payload.get("status") not in (None, "ALLOWED"):
                raise LinkedInUploadError("LinkedIn did not allow the uploaded image asset.")
            recipes = [
                recipe
                for recipe in payload["recipes"]
                if isinstance(recipe, dict) and recipe.get("recipe") == IMAGE_RECIPE
            ]
            if not recipes:
                raise LinkedInUploadError("LinkedIn did not return the required image recipe.")
            statuses = [recipe.get("status") for recipe in recipes]
            if all(status == "AVAILABLE" for status in statuses):
                return
            if any(
                status not in ("AVAILABLE", "NEW", "PROCESSING", "WAITING_UPLOAD")
                for status in statuses
            ):
                raise LinkedInUploadError("LinkedIn image processing failed.")
            if attempt < ASSET_POLL_ATTEMPTS - 1:
                time.sleep(ASSET_POLL_INTERVAL)
        raise LinkedInUploadError("LinkedIn image was not ready within the polling limit.")

    def upload_image(self, image_path: str | Path, *, alt_text: str = "") -> str:
        """Upload a validated image and return its ready digitalmedia asset URN.

        Alt text is applied by publish_post(image_alt_text=...), not asset registration.
        """
        data, content_type = _read_image(image_path)
        user_id = self._get_user_id()
        if not user_id:
            raise LinkedInUploadError("Unable to resolve the LinkedIn member for image upload.")
        response = self._upload_request(
            "POST",
            "https://api.linkedin.com/v2/assets?action=registerUpload",
            stage="registration",
            json={
                "registerUploadRequest": {
                    "recipes": [IMAGE_RECIPE],
                    "owner": f"urn:li:person:{user_id}",
                    "serviceRelationships": [
                        {"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}
                    ],
                    "supportedUploadMechanism": ["SYNCHRONOUS_UPLOAD"],
                }
            },
        )
        try:
            value = response.json()["value"]
            asset_urn = value["asset"]
            upload_url = value["uploadMechanism"][
                "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
            ]["uploadUrl"]
        except (ValueError, KeyError, TypeError):
            raise LinkedInUploadError(
                "LinkedIn returned invalid image registration data."
            ) from None
        asset_id = _image_asset_id(asset_urn)
        _validate_upload_url(upload_url)
        self._upload_request(
            "PUT", upload_url, stage="upload", data=data, headers={"Content-Type": content_type}
        )
        self._wait_for_image(asset_id)
        logger.info("LinkedIn image upload is ready.")
        return asset_urn

    def _extract_post_urn(self, response: requests.Response) -> str | None:
        candidates: list[str] = []

        header_id = response.headers.get("x-restli-id")
        if header_id:
            candidates.append(header_id)

        location = response.headers.get("location")
        if location:
            candidates.append(location.rsplit("/", 1)[-1])

        try:
            payload = response.json()
            if isinstance(payload, dict):
                post_id = payload.get("id")
                if isinstance(post_id, str):
                    candidates.append(post_id)
        except ValueError:
            pass

        for candidate in candidates:
            decoded = unquote(candidate).strip()
            if decoded.startswith("urn:li:"):
                return decoded
            if decoded.isdigit():
                return f"urn:li:ugcPost:{decoded}"

        return None

    def publish_post(
        self,
        description: str,
        *,
        image_asset_urn: str | None = None,
        image_alt_text: str = "",
    ) -> LinkedInPublishResult | None:
        """Publish once; ambiguous outcomes raise rather than invite automatic retries."""
        if image_asset_urn is not None:
            try:
                _image_asset_id(image_asset_urn)
            except LinkedInUploadError:
                logger.error("LinkedIn post rejected: invalid image asset identifier.")
                return None
        user_id = self._get_user_id()
        if not user_id:
            return None

        content = {
            "shareCommentary": {"text": description},
            "shareMediaCategory": "NONE",
        }
        if image_asset_urn is not None:
            content["shareMediaCategory"] = "IMAGE"
            content["media"] = [
                {
                    "status": "READY",
                    "media": image_asset_urn,
                    "description": {"text": image_alt_text},
                }
            ]
        payload = {
            "author": f"urn:li:person:{user_id}",
            "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": content},
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }

        try:
            resp = self.session.post(
                "https://api.linkedin.com/v2/ugcPosts",
                json=payload,
                timeout=15,
                allow_redirects=False,
            )
        except requests.RequestException:
            logger.error("LinkedIn post outcome is uncertain after a transport failure.")
            raise LinkedInPublishUncertain(
                "LinkedIn publication could not be confirmed. Do not automatically retry."
            ) from None
        if 500 <= resp.status_code < 600 or (
            200 <= resp.status_code < 300 and resp.status_code != 201
        ):
            logger.error("LinkedIn post outcome is uncertain (HTTP %s).", resp.status_code)
            raise LinkedInPublishUncertain(
                "LinkedIn publication could not be confirmed. Do not automatically retry."
            )
        if resp.status_code != 201:
            logger.error("LinkedIn post failed (HTTP %s).", resp.status_code)
            return None
        post_urn = self._extract_post_urn(resp)
        logger.info("LinkedIn post successful (HTTP 201).")
        return LinkedInPublishResult(status_code=resp.status_code, post_urn=post_urn)

    def post_comment(self, *, post_urn: str, comment_text: str) -> requests.Response | None:
        user_id = self._get_user_id()
        if not user_id:
            return None

        encoded_post_urn = quote(post_urn, safe="")
        payload = {
            "actor": f"urn:li:person:{user_id}",
            "message": {"text": comment_text},
        }

        try:
            response = self.session.post(
                f"https://api.linkedin.com/v2/socialActions/{encoded_post_urn}/comments",
                json=payload,
                timeout=15,
                allow_redirects=False,
            )
            if not 200 <= response.status_code < 300:
                logger.error("LinkedIn first comment failed (HTTP %s).", response.status_code)
                return None
            logger.info("LinkedIn first comment posted successfully.")
            return response
        except requests.RequestException:
            logger.error("LinkedIn first comment request failed.")
            return None
