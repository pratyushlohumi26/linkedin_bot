"""Exercise real workflow/SQLite logic with external service boundaries substituted.

Deterministic API doubles avoid billable image calls and public posts while
allowing timeout, cancellation, and partial-publication paths to be exercised.
Provider payload parsing and HTTP behavior are tested separately in client tests.
"""

from dataclasses import replace
from io import BytesIO
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from PIL import Image
from telebot import TeleBot
from telebot.types import CallbackQuery, Message

from telegram_bot.config import load_config
from telegram_bot.drafts import DraftConflict
from telegram_bot.handlers import DraftBotController
from telegram_bot.workflow import DISCLOSURE, DraftWorkflow, reviewed_post


def png_bytes():
    stream = BytesIO()
    Image.new("RGB", (128, 128), "teal").save(stream, format="PNG")
    return stream.getvalue()


class ServiceBoundary:
    def __init__(self):
        self.posts = []
        self.uploads = []
        self.generations = 0
        self.image_error = False
        self.post_error = False
        self.post_rejected = False
        self.upload_error = False
        self.missing_urn = False
        self.x_error = False
        self.comment_error = False
        self.thread = None
        self.block_image = None

    def generate_linkedin_variants(self, article, **kwargs):
        return {
            "A": "Practical on-device AI.",
            "B": "Keep AI processing local.",
            "C": "A story about local AI.",
        }

    def generate_x_thread(self, article):
        return {
            1: "Local inference can avoid sending inputs to a server.",
            2: "Review the trade-offs.",
        }

    def generate_image_brief(self, article, post, **kwargs):
        assert article and post
        return {
            "message": "Local AI",
            "concept": "Network within a laptop",
            "prompt": "Draw a laptop with a network, no text.",
            "alt_text": "A laptop containing a network",
        }

    def generate(self, prompt):
        self.generations += 1
        if self.block_image:
            self.block_image[0].set()
            self.block_image[1].wait(5)
        if self.image_error:
            raise RuntimeError("remote failure containing secret-which-must-not-reach-users")
        return png_bytes()

    def upload_image(self, image_path, **kwargs):
        if self.upload_error:
            raise RuntimeError("upload error")
        self.uploads.append(Path(image_path).read_bytes())
        return "urn:li:digitalmediaAsset:test"

    def publish_post(self, text, **kwargs):
        self.posts.append((text, kwargs))
        if self.post_error:
            raise TimeoutError("ambiguous transport timeout")
        if self.post_rejected:
            return None
        return SimpleNamespace(
            status_code=201, post_urn=None if self.missing_urn else "urn:li:share:123"
        )

    def post_prepared_thread(self, thread):
        self.thread = list(thread)
        if self.x_error:
            raise TimeoutError("thread partially posted")
        return "https://x.com/i/web/status/123"

    def generate_linkedin_first_comment(self, **kwargs):
        if self.comment_error:
            raise RuntimeError("comment failed")
        return "Additional context."

    def post_comment(self, **kwargs):
        return SimpleNamespace(status_code=201)


@pytest.fixture
def setup_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    for key, value in {
        "TELEGRAM_TOKEN": "123456:testing",
        "LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "test-only",
        "LINKEDIN_TOKEN": "test-only",
        "LINKEDIN_ENABLE_IMAGES": "true",
        "IMAGE_PROVIDER": "openai",
        "DRAFT_STORE_PATH": str(tmp_path / "drafts.sqlite3"),
        "PIPELINE_TELEMETRY_PATH": str(tmp_path / "telemetry.jsonl"),
        "LINKEDIN_ENABLE_FIRST_COMMENT": "false",
        "IMAGE_MAX_GENERATIONS": "3",
        "X_API_KEY": "test",
        "X_API_SECRET_KEY": "test",
        "X_ACCESS_TOKEN": "test",
        "X_ACCESS_TOKEN_SECRET": "test",
    }.items():
        monkeypatch.setenv(key, value)
    config = load_config()
    services = ServiceBoundary()
    flow = DraftWorkflow(
        config, generator=services, image_generator=services, linkedin=services, twitter=services
    )
    return flow, services


def selected(flow, feed="linkedin"):
    draft = flow.store.create(10, 20, feed)
    draft = flow.store.update(
        draft,
        status="source_review",
        changes={
            "article_text": "An article discussing local inference and data residency.",
            "source_url": "https://example.com/article",
        },
    )
    draft = flow.generate_variants(draft)
    return flow.select(draft, "B") if feed != "twitter" else draft


def with_image(flow):
    draft = selected(flow)
    return flow.generate_image(flow.make_brief(draft))


def test_nothing_posts_until_final_approval_and_exact_content_is_used(setup_flow):
    flow, api = setup_flow
    draft = with_image(flow)
    original = flow.image_path(draft).read_bytes()
    assert api.posts == []
    assert DISCLOSURE in draft.data["post_text"]
    approved_revision = draft.revision
    result = flow.publish(draft)
    assert result.status == "published"
    assert result.data["approved_revision"] == approved_revision
    assert result.data["linkedin_url"].endswith("urn:li:share:123/")
    assert api.uploads == [original]
    assert api.posts == [
        (
            draft.data["post_text"],
            {
                "image_asset_urn": "urn:li:digitalmediaAsset:test",
                "image_alt_text": draft.data["image_alt_text"],
            },
        )
    ]
    assert api.generations == 1
    with pytest.raises(DraftConflict):
        flow.publish(draft)
    with pytest.raises(DraftConflict):
        flow.publish(result)
    assert len(api.posts) == 1


def test_edit_invalidates_preview_and_requires_image_reapproval(setup_flow):
    flow, api = setup_flow
    old = with_image(flow)
    new = flow.edit(flow.request_edit(old, "text"), "A different angle on local inference.")
    assert not flow.image_matches(new)
    with pytest.raises(DraftConflict):
        flow.publish(old)
    with pytest.raises(ValueError, match="changed"):
        flow.publish(new)
    new = flow.keep_image(new)
    assert flow.image_matches(new)
    assert flow.publish(new).status == "published"
    assert api.generations == 1


def test_instruction_change_removal_and_cancel_do_not_publish(setup_flow):
    flow, api = setup_flow
    draft = with_image(flow)
    draft = flow.edit(flow.request_edit(draft, "idea"), "No laptop, abstract network instead.")
    assert not flow.image_matches(draft)
    draft = flow.remove_image(draft)
    assert draft.status == "review" and not draft.data["image_path"]
    assert api.posts == []
    cancelled = flow.cancel(draft)
    with pytest.raises(DraftConflict):
        flow.publish(cancelled)


def test_generation_limit_counts_failures_and_preserves_previous_image(setup_flow):
    flow, api = setup_flow
    draft = with_image(flow)
    old_path = draft.data["image_path"]
    api.image_error = True
    draft = flow.generate_image(draft)
    draft = flow.generate_image(draft)
    assert draft.data["image_generations"] == 3
    assert draft.data["image_path"] == old_path
    assert "secret-" not in draft.data["notice"]
    with pytest.raises(ValueError, match="limit"):
        flow.generate_image(draft)
    assert api.generations == 3


def test_cancelled_generation_cannot_replace_draft_or_leave_image(setup_flow):
    flow, api = setup_flow
    draft = flow.make_brief(selected(flow))
    api.block_image = (Event(), Event())
    errors = []

    def generate():
        try:
            flow.generate_image(draft)
        except DraftConflict as exc:
            errors.append(exc)

    thread = Thread(target=generate)
    thread.start()
    assert api.block_image[0].wait(3)
    busy = flow.store.get(draft.id, 10, 20)
    flow.cancel(busy)
    api.block_image[1].set()
    thread.join(5)
    assert errors and flow.store.get(draft.id, 10, 20).status == "cancelled"
    assert not list(flow.image_dir.rglob("*.png"))
    assert api.posts == []


def test_ambiguous_publish_is_persisted_and_never_retried(setup_flow):
    flow, api = setup_flow
    draft = selected(flow)
    api.post_error = True
    result = flow.publish(draft)
    assert result.status == "uncertain"
    assert result.data["linkedin_status"] == "uncertain"
    with pytest.raises(DraftConflict):
        flow.publish(result)
    assert len(api.posts) == 1


def test_upload_failure_is_safe_to_retry_and_does_not_post(setup_flow):
    flow, api = setup_flow
    draft = with_image(flow)
    api.upload_error = True
    result = flow.publish(draft)
    assert result.status == "review" and api.posts == []
    api.upload_error = False
    assert flow.publish(result).status == "published"


def test_known_rejection_reuses_uploaded_asset_after_fresh_review(setup_flow):
    flow, api = setup_flow
    draft = with_image(flow)
    api.post_rejected = True
    rejected = flow.publish(draft)
    assert rejected.status == "review"
    api.post_rejected = False
    assert flow.publish(rejected).status == "published"
    assert len(api.uploads) == 1


def test_missing_urn_and_first_comment_failure_do_not_republish(setup_flow):
    flow, api = setup_flow
    flow.config = replace(
        flow.config, linkedin_enable_first_comment=True, linkedin_first_comment_delay_seconds=0
    )
    flow.publisher.config = flow.config
    api.comment_error = True
    result = flow.publish(selected(flow))
    assert result.status == "published"
    assert result.data["first_comment_status"].startswith("failed")
    assert len(api.posts) == 1
    api.missing_urn = True
    result = flow.publish(selected(flow))
    assert result.status == "published" and result.data["linkedin_url"] is None
    with pytest.raises(DraftConflict):
        flow.publish(result)


def test_both_mode_preserves_linkedin_result_when_x_is_uncertain(setup_flow):
    flow, api = setup_flow
    draft = selected(flow, "both")
    api.x_error = True
    result = flow.publish(draft)
    assert result.status == "partial"
    assert result.data["linkedin_status"] == "published"
    assert result.data["twitter_status"] == "uncertain"
    assert api.thread == draft.data["x_thread"]
    with pytest.raises(DraftConflict):
        flow.publish(result)


def test_tampered_or_missing_image_cannot_publish(setup_flow):
    flow, api = setup_flow
    draft = with_image(flow)
    path = flow.image_path(draft)
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        flow.publish(draft)
    path.unlink()
    with pytest.raises(ValueError, match="missing"):
        flow.publish(draft)
    assert api.posts == []


def test_disabled_images_preserve_text_only_and_x_review(setup_flow):
    flow, api = setup_flow
    flow.config = replace(flow.config, images=replace(flow.config.images, enabled=False))
    draft = selected(flow)
    with pytest.raises(ValueError, match="disabled"):
        flow.make_brief(draft)
    assert flow.publish(draft).status == "published"
    assert api.uploads == []
    x_draft = selected(flow, "twitter")
    assert api.thread is None
    assert flow.publish(x_draft).status == "published"
    assert api.thread == x_draft.data["x_thread"]


def test_post_bounds_and_disclosure():
    assert reviewed_post(reviewed_post("Example")) == reviewed_post("Example")
    with pytest.raises(ValueError):
        reviewed_post("")
    with pytest.raises(ValueError):
        reviewed_post("x" * 3000)


class TelegramBoundary(TeleBot):
    """Replace only Telegram network sends; real handler registration is retained."""

    def __init__(self):
        super().__init__("123456:test-only", threaded=False)
        self.sent = []
        self.fail_photo = False

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append(("message", text, kwargs))

    def send_photo(self, chat_id, photo, **kwargs):
        if self.fail_photo:
            raise RuntimeError("Telegram media upload failed")
        self.sent.append(("photo", photo.read(), kwargs))

    def send_document(self, chat_id, document, **kwargs):
        self.sent.append(("document", document.read(), kwargs))

    def answer_callback_query(self, *args, **kwargs):
        pass


def callback_for(draft, action, *, user=20):
    return CallbackQuery.de_json(
        {
            "id": "test-callback",
            "from": {"id": user, "is_bot": False, "first_name": "Tester"},
            "chat_instance": "test",
            "data": f"d:{draft.id}:{draft.revision}:{action}",
            "message": {
                "message_id": 1,
                "date": 1,
                "chat": {"id": 10, "type": "private"},
                "text": "Draft controls",
            },
        }
    )


def buttons(bot):
    return [
        button
        for kind, _, kwargs in bot.sent
        if kind == "message"
        for row in getattr(kwargs.get("reply_markup"), "keyboard", [])
        for button in row
    ]


def test_real_telegram_selection_and_preview_do_not_publish(setup_flow):
    flow, api = setup_flow
    bot = TelegramBoundary()
    ui = DraftBotController(bot, flow.config, workflow=flow)
    draft = flow.store.create(10, 20, "linkedin")
    draft = flow.store.update(
        draft,
        status="variants",
        changes={
            "article_text": "Local AI article",
            "variants": {"A": "a" * 1200, "B": "B", "C": "C"},
        },
    )
    ui.callback(callback_for(draft, "A"))
    current = flow.store.get(draft.id, 10, 20)
    assert current.status == "review"
    assert api.posts == []
    assert any("a" * 1200 in text for kind, text, _ in bot.sent if kind == "message")
    assert any("Publish" in button.text for button in buttons(bot))
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons(bot))
    ui.close()


def test_telegram_shows_photo_full_text_and_submits_same_version(setup_flow):
    flow, api = setup_flow
    bot = TelegramBoundary()
    ui = DraftBotController(bot, flow.config, workflow=flow)
    draft = with_image(flow)
    ui.present(draft)
    assert bot.sent[1][0] == "photo"  # notice precedes the photo
    assert any(kind == "message" and draft.data["post_text"] in text for kind, text, _ in bot.sent)
    assert all(len(kwargs.get("caption", "")) <= 1024 for _, _, kwargs in bot.sent)
    ui.callback(callback_for(draft, "publish"))
    ui.close()
    result = flow.store.get(draft.id, 10, 20)
    assert result.status == "published"
    assert len(api.posts) == 1
    assert any(
        result.data["linkedin_url"] in text for kind, text, _ in bot.sent if kind == "message"
    )


def test_telegram_blocks_other_users_and_stale_buttons(setup_flow):
    flow, api = setup_flow
    bot = TelegramBoundary()
    ui = DraftBotController(bot, flow.config, workflow=flow)
    draft = selected(flow)
    ui.callback(callback_for(draft, "publish", user=21))
    flow.edit(flow.request_edit(draft, "text"), "Revised post")
    ui.callback(callback_for(draft, "publish"))
    ui.close()
    assert api.posts == []
    assert any("outdated" in text for kind, text, _ in bot.sent if kind == "message")


def test_failed_photo_delivery_never_sends_publish_controls(setup_flow):
    flow, api = setup_flow
    bot = TelegramBoundary()
    ui = DraftBotController(bot, flow.config, workflow=flow)
    draft = with_image(flow)
    bot.fail_photo = True
    ui.safe_present(draft)
    assert not buttons(bot)
    assert api.posts == []
    assert flow.store.get(draft.id, 10, 20).status == "review"
    ui.close()


def test_resume_survives_a_new_controller_and_uses_saved_image(setup_flow):
    flow, api = setup_flow
    draft = with_image(flow)
    before = flow.image_path(draft).read_bytes()
    bot = TelegramBoundary()
    ui = DraftBotController(bot, flow.config, workflow=flow)
    message = Message.de_json(
        {
            "message_id": 3,
            "date": 1,
            "chat": {"id": 10, "type": "private"},
            "from": {"id": 20, "is_bot": False, "first_name": "Tester"},
            "text": "/resume",
        }
    )
    bot.process_new_messages([message])
    ui.close()
    assert any(kind == "photo" and data == before for kind, data, _ in bot.sent)
    assert api.posts == [] and api.generations == 1


def test_missing_image_preview_offers_remove_but_not_publish(setup_flow):
    flow, api = setup_flow
    bot = TelegramBoundary()
    ui = DraftBotController(bot, flow.config, workflow=flow)
    draft = with_image(flow)
    flow.image_path(draft).unlink()
    ui.safe_present(draft)
    assert any(button.text == "Remove image" for button in buttons(bot))
    assert not any("Publish" in button.text for button in buttons(bot))
    ui.callback(callback_for(draft, "remove"))
    assert any("Publish" in button.text for button in buttons(bot))
    assert api.posts == []
    ui.close()


def test_qualified_start_post_command_shows_destinations(setup_flow):
    flow, _ = setup_flow
    bot = TelegramBoundary()
    ui = DraftBotController(bot, flow.config, workflow=flow)
    message = Message.de_json(
        {
            "message_id": 4,
            "date": 1,
            "chat": {"id": 10, "type": "private"},
            "from": {"id": 20, "is_bot": False, "first_name": "Tester"},
            "text": "/start_post@testing_bot",
        }
    )
    bot.process_new_messages([message])
    ui.close()
    assert {button.callback_data for button in buttons(bot)} == {
        "dest:linkedin",
        "dest:twitter",
        "dest:both",
    }
