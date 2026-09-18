from __future__ import annotations

import json
import socket
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
import requests
from PIL import Image
from requests.adapters import HTTPAdapter

from telegram_bot import linkedin_client as linkedin
from telegram_bot.linkedin_client import LinkedinAutomate, build_linkedin_post_url


def _response_with(
    *, headers: dict[str, str] | None = None, payload: str | None = None
) -> requests.Response:
    response = requests.Response()
    response.status_code = 201
    response.headers.update(headers or {})
    if payload is not None:
        response._content = payload.encode("utf-8")
        response.headers.setdefault("Content-Type", "application/json")
    return response


def test_extract_post_urn_from_header() -> None:
    client = LinkedinAutomate("dummy")
    response = _response_with(headers={"x-restli-id": "urn:li:ugcPost:123"})

    assert client._extract_post_urn(response) == "urn:li:ugcPost:123"


def test_extract_post_urn_from_numeric_location() -> None:
    client = LinkedinAutomate("dummy")
    response = _response_with(headers={"location": "https://api.linkedin.com/v2/ugcPosts/98765"})

    assert client._extract_post_urn(response) == "urn:li:ugcPost:98765"


def test_extract_post_urn_from_payload() -> None:
    client = LinkedinAutomate("dummy")
    response = _response_with(payload='{"id": "urn:li:ugcPost:111"}')

    assert client._extract_post_urn(response) == "urn:li:ugcPost:111"


def test_build_linkedin_post_url_from_urn() -> None:
    assert (
        build_linkedin_post_url("urn:li:ugcPost:12345")
        == "https://www.linkedin.com/feed/update/urn:li:ugcPost:12345/"
    )


def test_build_linkedin_post_url_returns_none_for_invalid_value() -> None:
    assert build_linkedin_post_url("https://example.com") is None
    assert build_linkedin_post_url(None) is None


ASSET = "urn:li:digitalmediaAsset:C123abc"
RECIPE = "urn:li:digitalmediaRecipe:feedshare-image"
UPLOAD_URL = "https://upload.linkedin.com/media-upload?signature=PRIVATE-SIGNATURE"


class LoopbackAdapter(HTTPAdapter):
    """Replace only the network destination, retaining real requests HTTP behavior."""

    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.calls = []
        self.failures = {}

    def send(self, request, **kwargs):
        original_url = request.url
        parsed = urlsplit(original_url)
        self.calls.append((request.method, original_url))
        failure = self.failures.get((request.method, parsed.path))
        if failure:
            raise failure
        local_request = request.copy()
        local_request.url = self.base_url + parsed.path
        if parsed.query:
            local_request.url += "?" + parsed.query
        local_request.headers["X-Test-Original-URL"] = original_url
        kwargs["proxies"] = {}
        return super().send(local_request, **kwargs)


@pytest.fixture
def api():
    replies = deque()
    received = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append(
                {
                    "method": self.command,
                    "url": self.headers["X-Test-Original-URL"],
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body,
                }
            )
            status, payload, headers = replies.popleft() if replies else (500, {}, {})
            if status == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = handle_request

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    client = LinkedinAutomate("PRIVATE-TOKEN")
    client.session.trust_env = False
    adapter = LoopbackAdapter(f"http://127.0.0.1:{server.server_port}")
    client.session.mount("https://", adapter)
    client.session.mount("http://", adapter)

    class API:
        def reply(self, status=200, payload=None, headers=None):
            replies.append((status, payload, headers or {}))

    api = API()
    api.client = client
    api.received = received
    api.adapter = adapter
    try:
        yield api
    finally:
        client.session.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.fixture
def image_path(tmp_path):
    path = tmp_path / "approved.png"
    Image.new("RGB", (24, 32), "blue").save(path)
    return path


def registration(url=UPLOAD_URL, asset=ASSET):
    return {
        "value": {
            "asset": asset,
            "uploadMechanism": {
                "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest": {"uploadUrl": url}
            },
        }
    }


def ready(status="AVAILABLE", recipe=RECIPE, asset_status="ALLOWED"):
    return {"status": asset_status, "recipes": [{"recipe": recipe, "status": status}]}


def queue_upload(api, *, url=UPLOAD_URL, asset=ASSET):
    api.reply(payload={"sub": "member123"})
    api.reply(payload=registration(url, asset))
    api.reply(201)


def assert_safe(text):
    for private in ("PRIVATE-TOKEN", "PRIVATE-SIGNATURE", "PRIVATE-RESPONSE"):
        assert private not in text


def test_upload_registers_binary_then_waits_for_recipe(api, image_path, monkeypatch):
    monkeypatch.setattr(linkedin.time, "sleep", lambda _: None)
    queue_upload(api)
    api.reply(payload=ready("PROCESSING"))
    api.reply(payload=ready())
    assert api.client.upload_image(image_path, alt_text="A blue rectangle") == ASSET
    assert [(r["method"], urlsplit(r["url"]).path) for r in api.received] == [
        ("GET", "/v2/userinfo"),
        ("POST", "/v2/assets"),
        ("PUT", "/media-upload"),
        ("GET", "/v2/assets/C123abc"),
        ("GET", "/v2/assets/C123abc"),
    ]
    assert api.received[1]["path"] == "/v2/assets?action=registerUpload"
    assert json.loads(api.received[1]["body"]) == {
        "registerUploadRequest": {
            "recipes": [RECIPE],
            "owner": "urn:li:person:member123",
            "serviceRelationships": [
                {"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}
            ],
            "supportedUploadMechanism": ["SYNCHRONOUS_UPLOAD"],
        }
    }
    upload = api.received[2]
    assert upload["body"] == image_path.read_bytes()
    assert upload["headers"]["Content-Type"] == "image/png"
    assert upload["headers"]["Authorization"] == "Bearer PRIVATE-TOKEN"


@pytest.mark.parametrize(
    "url",
    [
        "https://linkedin.com/upload",
        "https://www.linkedin.com/upload",
        "https://a.b.linkedin.com:443/upload",
        "https://UPLOAD.LINKEDIN.COM/upload",
    ],
)
def test_upload_allows_only_trusted_https_destinations(api, image_path, url):
    queue_upload(api, url=url)
    api.reply(payload=ready())
    assert api.client.upload_image(str(image_path)) == ASSET


@pytest.mark.parametrize(
    "url",
    [
        "http://upload.linkedin.com/upload",
        "https://linkedin.com.evil.example/upload",
        "https://evillinkedin.com/upload",
        "https://evil.example/linkedin.com/upload",
        "https://127.0.0.1/upload",
        "https://[::1]/upload",
        "//upload.linkedin.com/upload",
        "https://user:PRIVATE-TOKEN@upload.linkedin.com/upload",
        "https://linkedin.com@evil.example/upload",
        "https://upload.linkedin.com:8443/upload",
        "https://upload.linkedin.com/upload#fragment",
        "https://upload.linkedin.com\\@evil.example/upload",
        "https://upload.linkedin.com./upload",
        "https://upload.linkedin.com/\nsecret",
        "https://upload.linkedin.com:bad/upload",
        "https://.linkedin.com/upload",
        "https://bad_label.linkedin.com/upload",
        "",
        None,
    ],
)
def test_upload_rejects_untrusted_urls_without_put(api, image_path, url, caplog):
    api.reply(payload={"sub": "member123"})
    api.reply(payload=registration(url))
    with pytest.raises(linkedin.LinkedInUploadError) as error:
        api.client.upload_image(image_path)
    assert len(api.received) == 2
    assert_safe(str(error.value) + caplog.text)


@pytest.mark.parametrize(
    "asset",
    [
        None,
        "",
        "urn:li:image:123",
        "urn:li:digitalmediaAsset:../userinfo",
        "urn:li:digitalmediaAsset:abc?x=1",
    ],
)
def test_upload_rejects_invalid_asset_identifiers(api, image_path, asset):
    api.reply(payload={"sub": "member123"})
    api.reply(payload=registration(asset=asset))
    with pytest.raises(linkedin.LinkedInUploadError):
        api.client.upload_image(image_path)
    assert len(api.received) == 2


@pytest.mark.parametrize("stage", ["identity", "register", "put", "asset"])
def test_upload_does_not_follow_redirects(api, image_path, stage):
    if stage != "identity":
        api.reply(payload={"sub": "member123"})
    if stage in {"put", "asset"}:
        api.reply(payload=registration())
    if stage == "asset":
        api.reply(201)
    api.reply(307, headers={"Location": "https://evil.example/PRIVATE-SIGNATURE"})
    with pytest.raises(linkedin.LinkedInUploadError):
        api.client.upload_image(image_path)
    assert len(api.received) == {"identity": 1, "register": 2, "put": 3, "asset": 4}[stage]


@pytest.mark.parametrize("stage", ["identity", "register", "put", "asset"])
@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_upload_http_failures_are_safe(api, image_path, stage, status, caplog):
    if stage != "identity":
        api.reply(payload={"sub": "member123"})
    if stage in {"put", "asset"}:
        api.reply(payload=registration())
    if stage == "asset":
        api.reply(201)
    api.reply(status, {"error": "PRIVATE-RESPONSE PRIVATE-TOKEN"})
    with pytest.raises(linkedin.LinkedInUploadError) as error:
        api.client.upload_image(image_path)
    assert_safe(str(error.value) + caplog.text)
    assert all(r["path"] != "/v2/ugcPosts" for r in api.received)


@pytest.mark.parametrize(
    "payload", [None, [], {}, {"value": []}, {"value": {"asset": ASSET}}, b"PRIVATE-RESPONSE"]
)
def test_upload_rejects_malformed_registration(api, image_path, payload):
    api.reply(payload={"sub": "member123"})
    api.reply(payload=payload)
    with pytest.raises(linkedin.LinkedInUploadError) as error:
        api.client.upload_image(image_path)
    assert_safe(str(error.value))
    assert len(api.received) == 2


@pytest.mark.parametrize(
    "payload",
    [
        ready("CLIENT_ERROR"),
        ready("SERVER_ERROR"),
        ready(asset_status="BLOCKED"),
        None,
        [],
        {},
        {"recipes": []},
        {"recipes": [None]},
        ready(recipe="urn:li:digitalmediaRecipe:unrelated"),
        b"PRIVATE-RESPONSE",
    ],
)
def test_upload_never_returns_unready_assets(api, image_path, payload, monkeypatch):
    monkeypatch.setattr(linkedin.time, "sleep", lambda _: None)
    queue_upload(api)
    for _ in range(linkedin.ASSET_POLL_ATTEMPTS):
        api.reply(payload=payload)
    with pytest.raises(linkedin.LinkedInUploadError):
        api.client.upload_image(image_path)
    assert len(api.received) <= 3 + linkedin.ASSET_POLL_ATTEMPTS


def test_upload_readiness_polling_is_bounded(api, image_path, monkeypatch):
    delays = []
    monkeypatch.setattr(linkedin.time, "sleep", delays.append)
    queue_upload(api)
    for _ in range(linkedin.ASSET_POLL_ATTEMPTS):
        api.reply(payload=ready("PROCESSING"))
    with pytest.raises(linkedin.LinkedInUploadError, match="ready"):
        api.client.upload_image(image_path)
    assert len(api.received) == 3 + linkedin.ASSET_POLL_ATTEMPTS
    assert len(delays) == linkedin.ASSET_POLL_ATTEMPTS - 1
    assert all(0 < delay <= 5 for delay in delays)


def test_upload_accepts_jpeg_from_content_not_filename(api, image_path):
    Image.new("RGB", (24, 32), "blue").save(image_path, format="JPEG")
    queue_upload(api)
    api.reply(payload=ready())
    assert api.client.upload_image(image_path) == ASSET
    assert api.received[2]["headers"]["Content-Type"] == "image/jpeg"


@pytest.mark.parametrize(
    "invalid",
    ["missing", "empty", "corrupt", "truncated", "gif", "bytes", "pixels", "dimension", "animated"],
)
def test_upload_validates_images_before_network(api, image_path, invalid, monkeypatch):
    if invalid == "missing":
        image_path.unlink()
    elif invalid == "empty":
        image_path.write_bytes(b"")
    elif invalid == "corrupt":
        image_path.write_bytes(b"PRIVATE-RESPONSE not an image")
    elif invalid == "truncated":
        image_path.write_bytes(image_path.read_bytes()[:45])
    elif invalid == "gif":
        Image.new("RGB", (24, 32)).save(image_path, format="GIF")
    elif invalid == "bytes":
        monkeypatch.setattr(linkedin, "MAX_IMAGE_BYTES", 10)
    elif invalid == "pixels":
        monkeypatch.setattr(linkedin, "MAX_IMAGE_PIXELS", 100)
    elif invalid == "dimension":
        monkeypatch.setattr(linkedin, "MAX_IMAGE_DIMENSION", 20)
    elif invalid == "animated":
        Image.new("RGB", (24, 32), "red").save(
            image_path,
            format="PNG",
            save_all=True,
            append_images=[Image.new("RGB", (24, 32), "blue")],
        )
    with pytest.raises(linkedin.LinkedInUploadError) as error:
        api.client.upload_image(image_path)
    assert_safe(str(error.value))
    assert api.received == []


def test_upload_fails_closed_without_pillow(api, image_path, monkeypatch):
    monkeypatch.setattr(linkedin, "Image", None)
    with pytest.raises(linkedin.LinkedInUploadError, match="Pillow"):
        api.client.upload_image(image_path)
    assert api.received == []


@pytest.mark.parametrize(
    "path, method",
    [
        ("/v2/userinfo", "GET"),
        ("/v2/assets", "POST"),
        ("/media-upload", "PUT"),
        ("/v2/assets/C123abc", "GET"),
    ],
)
def test_upload_transport_failures_are_safe(api, image_path, path, method, caplog):
    queue_upload(api)
    # Deterministic timeouts require a transport-boundary fault, not a live network.
    api.adapter.failures[(method, path)] = requests.Timeout("PRIVATE-TOKEN " + UPLOAD_URL)
    with pytest.raises(linkedin.LinkedInUploadError) as error:
        api.client.upload_image(image_path)
    assert error.value.__cause__ is None
    assert_safe(str(error.value) + caplog.text)


@pytest.mark.parametrize("asset, alt_text", [(None, ""), (ASSET, "A blue rectangle"), (ASSET, "")])
def test_publish_text_or_image_payload(api, asset, alt_text):
    api.reply(payload={"sub": "member123"})
    api.reply(201, {"id": "urn:li:share:123"})
    result = api.client.publish_post(
        "Approved caption", image_asset_urn=asset, image_alt_text=alt_text
    )
    assert result == linkedin.LinkedInPublishResult(201, "urn:li:share:123")
    assert len(api.received) == 2
    payload = json.loads(api.received[1]["body"])
    content = payload["specificContent"]["com.linkedin.ugc.ShareContent"]
    assert payload["author"] == "urn:li:person:member123"
    assert payload["lifecycleState"] == "PUBLISHED"
    assert payload["visibility"] == {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
    assert content["shareCommentary"] == {"text": "Approved caption"}
    if asset:
        assert content["shareMediaCategory"] == "IMAGE"
        assert content["media"] == [
            {"status": "READY", "media": ASSET, "description": {"text": alt_text}}
        ]
    else:
        assert content["shareMediaCategory"] == "NONE"
        assert "media" not in content


def test_publish_old_positional_api_and_missing_urn_remain_success(api):
    api.reply(payload={"sub": "member123"})
    api.reply(201, b"not json")
    assert api.client.publish_post("Approved caption") == linkedin.LinkedInPublishResult(201, None)
    assert len(api.received) == 2


@pytest.mark.parametrize("status", [301, 307, 400, 401, 403, 409, 422, 429])
def test_publish_definite_non201_failures_are_not_retried(api, status, caplog):
    api.reply(payload={"sub": "member123"})
    api.reply(
        status,
        {"error": "PRIVATE-RESPONSE"},
        {"Location": "https://evil.example/PRIVATE-SIGNATURE"},
    )
    assert api.client.publish_post("Approved caption") is None
    assert len(api.received) == 2
    assert_safe(caplog.text)


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
def test_publish_5xx_is_uncertain_without_retry(api, status, caplog):
    api.reply(payload={"sub": "member123"})
    api.reply(status, {"error": "PRIVATE-RESPONSE"})
    with pytest.raises(linkedin.LinkedInPublishUncertain) as error:
        api.client.publish_post("Approved caption")
    assert len(api.received) == 2
    assert_safe(str(error.value) + caplog.text)


def test_publish_connection_lost_after_request_is_uncertain(api, caplog):
    api.reply(payload={"sub": "member123"})
    api.reply("disconnect")
    with pytest.raises(linkedin.LinkedInPublishUncertain) as error:
        api.client.publish_post("Approved caption")
    assert len(api.received) == 2
    assert error.value.__cause__ is None
    assert_safe(str(error.value) + caplog.text)


@pytest.mark.parametrize(
    "failure",
    [requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError],
)
def test_publish_transport_failure_is_uncertain_without_retry(api, failure, caplog):
    api.reply(payload={"sub": "member123"})
    api.adapter.failures[("POST", "/v2/ugcPosts")] = failure("PRIVATE-TOKEN " + UPLOAD_URL)
    with pytest.raises(linkedin.LinkedInPublishUncertain) as error:
        api.client.publish_post("Approved caption")
    assert len(api.adapter.calls) == 2
    assert error.value.__cause__ is None
    assert_safe(str(error.value) + caplog.text)


@pytest.mark.parametrize(
    "status, payload",
    [(503, {}), (200, []), (200, {}), (200, {"sub": 123}), (200, b"PRIVATE-RESPONSE")],
)
def test_identity_failures_are_not_uncertain(api, status, payload, caplog):
    api.reply(status, payload)
    assert api.client.publish_post("Approved caption") is None
    assert len(api.received) == 1
    assert_safe(caplog.text)


def test_identity_timeout_does_not_attempt_publish(api, caplog):
    api.adapter.failures[("GET", "/v2/userinfo")] = requests.Timeout("PRIVATE-TOKEN")
    assert api.client.publish_post("Approved caption") is None
    assert len(api.adapter.calls) == 1
    assert_safe(caplog.text)


def test_comment_preserves_payload_and_return_type(api):
    api.reply(payload={"sub": "member123"})
    api.reply(201, {"id": "comment"})
    response = api.client.post_comment(post_urn="urn:li:share:123", comment_text="Approved comment")
    assert isinstance(response, requests.Response)
    assert response.status_code == 201
    assert api.received[1]["path"] == "/v2/socialActions/urn%3Ali%3Ashare%3A123/comments"
    assert json.loads(api.received[1]["body"]) == {
        "actor": "urn:li:person:member123",
        "message": {"text": "Approved comment"},
    }


def test_comment_failure_logs_are_safe(api, caplog):
    api.reply(payload={"sub": "member123"})
    path = "/v2/socialActions/urn%3Ali%3Ashare%3A123/comments"
    api.adapter.failures[("POST", path)] = requests.ConnectionError("PRIVATE-TOKEN " + UPLOAD_URL)
    assert api.client.post_comment(post_urn="urn:li:share:123", comment_text="Comment") is None
    assert_safe(caplog.text)


@pytest.mark.parametrize("status", [200, 202, 204])
def test_publish_unexpected_2xx_is_not_treated_as_safe_to_retry(api, status):
    api.reply(payload={"sub": "member123"})
    api.reply(status)
    with pytest.raises(linkedin.LinkedInPublishUncertain):
        api.client.publish_post("Approved caption")
    assert len(api.received) == 2


@pytest.mark.parametrize("asset", ["", "urn:li:image:123", "PRIVATE-TOKEN"])
def test_publish_invalid_image_asset_is_rejected_without_network(api, asset, caplog):
    assert api.client.publish_post("Approved caption", image_asset_urn=asset) is None
    assert api.received == []
    assert_safe(caplog.text)


def test_text_publish_does_not_require_pillow(api, monkeypatch):
    monkeypatch.setattr(linkedin, "Image", None)
    api.reply(payload={"sub": "member123"})
    api.reply(201, {}, {"x-restli-id": "urn:li:share:123"})
    assert api.client.publish_post("Text only") == linkedin.LinkedInPublishResult(
        201, "urn:li:share:123"
    )


@pytest.mark.parametrize("pixel_limit", [500, 100])
def test_upload_rejects_pillow_decompression_warnings_and_errors(
    api, image_path, monkeypatch, caplog, pixel_limit
):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", pixel_limit)
    with pytest.raises(linkedin.LinkedInUploadError) as error:
        api.client.upload_image(image_path)
    assert api.received == []
    assert_safe(str(error.value) + caplog.text)


def test_upload_returns_only_when_all_matching_recipes_are_available(api, image_path):
    queue_upload(api)
    payload = ready()
    payload["recipes"].append({"recipe": RECIPE, "status": "CLIENT_ERROR"})
    api.reply(payload=payload)
    with pytest.raises(linkedin.LinkedInUploadError):
        api.client.upload_image(image_path)
    assert len(api.received) == 4


@pytest.mark.parametrize("status", [307, 400, 403, 500])
def test_comment_http_failures_are_isolated_and_safe(api, status, caplog):
    api.reply(payload={"sub": "member123"})
    api.reply(
        status,
        {"error": "PRIVATE-RESPONSE"},
        {"Location": "https://evil.example/PRIVATE-SIGNATURE"},
    )
    assert api.client.post_comment(post_urn="urn:li:share:123", comment_text="Comment") is None
    assert len(api.received) == 2
    assert_safe(caplog.text)
