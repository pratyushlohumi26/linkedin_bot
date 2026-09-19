"""Review-first draft operations, independent of Telegram transport."""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from uuid import uuid4

from telegram_bot.article_extractor import pasted_article
from telegram_bot.config import AppConfig
from telegram_bot.drafts import TERMINAL_STATUSES, Draft, DraftConflict, DraftStore
from telegram_bot.image_generator import ImageGenerator, validate_image_bytes
from telegram_bot.linkedin_client import LinkedinAutomate
from telegram_bot.publisher import DraftPublisher
from telegram_bot.scraper import ArticleScraper, ScrapeResult, source_result
from telegram_bot.summarizer import ContentGenerator
from telegram_bot.telemetry import TelemetryLogger
from telegram_bot.twitter_client import prepare_thread

logger = logging.getLogger(__name__)


def reviewed_post(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError("Post text cannot be empty.")
    if len(text) > 3000:
        raise ValueError("LinkedIn text must fit within 3,000 characters.")
    return text


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class DraftWorkflow:
    def __init__(
        self,
        config: AppConfig,
        *,
        store=None,
        generator=None,
        image_generator=None,
        linkedin=None,
        twitter=None,
        scraper=None,
    ):
        self.config = config
        self.scraper = scraper or ArticleScraper(config.scraper)
        self.store = store or DraftStore(config.draft_store_path)
        self.generator = generator or ContentGenerator(config.llm)
        self.image_generator = image_generator or (
            ImageGenerator(config.images) if config.images.enabled else None
        )
        self.linkedin = linkedin or LinkedinAutomate(config.linkedin_token or "")
        self.publisher = DraftPublisher(config, self.store, self.generator, self.linkedin, twitter)
        self.telemetry = TelemetryLogger(config.telemetry_log_path)
        self.image_dir = self.store.path.parent / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)

    def cleanup(self):
        for draft_id in self.store.expire(self.config.draft_retention_days):
            directory = self.image_dir / draft_id
            if directory.is_dir() and not directory.is_symlink():
                shutil.rmtree(directory)

    @staticmethod
    def require(draft: Draft, *statuses: str):
        if draft.status not in statuses:
            raise DraftConflict("That action is not available for this draft. Use /resume.")

    def fail_generation(self, draft: Draft, status: str, notice: str) -> Draft:
        return self.store.update(draft, status=status, changes={"notice": notice})

    @staticmethod
    def _clear_source() -> dict:
        return {
            key: None
            for key in (
                "article_text",
                "source_metadata",
                "source_hash",
                "source_chunks",
                "source_session",
                "variants",
                "x_thread",
                "post_text",
                "selected_key",
                "brief",
                "image_path",
                "image_sha256",
                "image_for_text_hash",
                "image_alt_text",
                "image_asset_urn",
                "image_instructions",
                "image_needs_review",
                "approved_revision",
                "linkedin_status",
                "twitter_status",
                "linkedin_urn",
                "linkedin_url",
                "twitter_url",
                "first_comment_status",
            )
        }

    def replace_source(self, draft: Draft) -> Draft:
        self.require(
            draft, "source_review", "source_recovery", "awaiting_source_text", "review", "variants"
        )
        return self.store.update(
            draft,
            status="awaiting_url",
            changes={
                **self._clear_source(),
                "source_url": "",
                "notice": "Send a new public URL. Previous generated content and approvals were invalidated.",
            },
        )

    def scrape(self, draft: Draft, url: str) -> Draft:
        self.require(draft, "awaiting_url", "source_recovery")
        if draft.status == "source_recovery" and not draft.data.get("source_metadata", {}).get(
            "retryable"
        ):
            raise ValueError("This failure is not retryable. Paste the text or choose another URL.")
        draft = self.store.update(
            draft,
            status="scraping",
            changes={**self._clear_source(), "source_url": url, "notice": ""},
        )
        result = self.scraper.scrape(url)
        try:
            self.telemetry.record(
                "source_fetch",
                draft_id=draft.id,
                status=result.status,
                method=result.method,
                elapsed_seconds=result.elapsed_seconds,
                attempts=result.attempts,
                text_chars=len(result.text),
                cached=result.cached,
            )
        except Exception:
            logger.warning("Source telemetry unavailable; retaining the retrieval result")
        if result.usable:
            return self._accept_source(draft, result)
        return self.store.update(
            draft,
            status="source_recovery",
            changes={"source_metadata": result.metadata(), "notice": result.error},
        )

    def _accept_source(self, draft: Draft, result: ScrapeResult) -> Draft:
        return self.store.update(
            draft,
            status="source_review",
            changes={
                "article_text": result.text,
                "source_url": result.requested_url,
                "source_metadata": result.metadata(),
                "source_hash": result.content_hash,
                "source_chunks": None,
                "source_session": None,
                "notice": "Inspect the recovered source before generating drafts. Nothing has been published.",
            },
        )

    def begin_source_text(self, draft: Draft) -> Draft:
        self.require(draft, "awaiting_url", "source_recovery", "source_review")
        return self.store.update(
            draft,
            status="awaiting_source_text",
            changes={
                **self._clear_source(),
                "source_session": uuid4().hex,
                "source_chunks": [],
                "notice": "Paste the article in one or more messages, or upload a UTF-8 .txt file. Press Done when complete.",
            },
        )

    def append_source_text(self, draft: Draft, text: str, *, chunk_id: int) -> Draft:
        self.require(draft, "awaiting_source_text")
        clean = pasted_article(text, max_text_chars=self.config.scraper.max_text_chars).text
        session = draft.data.get("source_session")
        for _ in range(5):
            self.require(draft, "awaiting_source_text")
            if draft.data.get("source_session") != session:
                raise DraftConflict(
                    "Source collection changed. Use /resume before sending more text."
                )
            chunks = list(draft.data.get("source_chunks") or [])
            if any(chunk["id"] == chunk_id for chunk in chunks):
                return draft
            chunks.append({"id": chunk_id, "text": clean})
            chunks.sort(key=lambda item: item["id"])
            if (
                len(chunks) > 100
                or len("\n\n".join(chunk["text"] for chunk in chunks))
                > self.config.scraper.max_text_chars
            ):
                raise ValueError(
                    f"Source exceeds the {self.config.scraper.max_text_chars:,}-character/100-chunk limit. Start again with a shorter article."
                )
            try:
                return self.store.update(
                    draft,
                    changes={
                        "source_chunks": chunks,
                        "notice": "Text saved. Add more or press Done to review the source.",
                    },
                )
            except DraftConflict:
                draft = self.store.get(draft.id, draft.chat_id, draft.user_id)
        raise DraftConflict("Source changed repeatedly. Use /resume and resend the last chunk.")

    def finish_source_text(self, draft: Draft) -> Draft:
        self.require(draft, "awaiting_source_text")
        text = "\n\n".join(chunk["text"] for chunk in (draft.data.get("source_chunks") or []))
        article = pasted_article(text, max_text_chars=self.config.scraper.max_text_chars)
        return self._accept_source(
            draft, source_result(article, draft.data.get("source_url", ""), method="user_supplied")
        )

    def generate_variants(self, draft: Draft) -> Draft:
        self.require(draft, "source_review", "variants")
        previous = draft.status
        draft = self.store.update(draft, status="generating_text", changes={"notice": ""})
        try:
            feed = draft.data["feed_type"]
            variants = (
                self.generator.generate_linkedin_variants(
                    draft.data["article_text"],
                    core_hashtags=self.config.linkedin_hashtag_core,
                    secondary_hashtags=self.config.linkedin_hashtag_secondary,
                )
                if feed != "twitter"
                else {}
            )
            thread = (
                prepare_thread(self.generator.generate_x_thread(draft.data["article_text"]))
                if feed in {"twitter", "both"}
                else []
            )
        except Exception as exc:
            logger.warning("Text generation failed (%s)", type(exc).__name__)
            return self.fail_generation(
                draft, previous, "Draft generation failed. You can retry; nothing was posted."
            )
        return self.store.update(
            draft,
            status="variants" if variants else "review",
            changes={"variants": variants, "x_thread": thread},
        )

    def select(self, draft: Draft, key: str) -> Draft:
        self.require(draft, "variants")
        text = draft.data.get("variants", {}).get(key)
        if not text:
            raise ValueError("Choose an available variant.")
        return self.store.update(
            draft,
            status="review",
            changes={
                "post_text": reviewed_post(text),
                "selected_key": key,
                "notice": "Review the full text. Nothing has been published.",
            },
        )

    def request_edit(self, draft: Draft, kind: str) -> Draft:
        self.require(draft, "review")
        if kind not in {"text", "idea"} or draft.data["feed_type"] == "twitter":
            raise ValueError("This edit is not supported for this draft.")
        if kind == "idea" and not self.config.images.enabled:
            raise ValueError("Image generation is disabled.")
        return self.store.update(draft, status="editing_" + kind)

    def edit(self, draft: Draft, text: str) -> Draft:
        self.require(draft, "editing_text", "editing_idea")
        if draft.status == "editing_text":
            changes = {
                "post_text": reviewed_post(text),
                "brief": None,
                "notice": "Text updated. Review again; regenerate or explicitly keep an existing image.",
            }
        else:
            if not text.strip() or len(text) > 1000:
                raise ValueError("Image instructions must be 1–1,000 characters.")
            changes = {
                "image_instructions": text.strip(),
                "brief": None,
                "image_needs_review": True,
                "notice": "Image instructions updated. Generate an image or explicitly keep the previous image.",
            }
        return self.store.update(draft, status="review", changes=changes)

    def make_brief(self, draft: Draft) -> Draft:
        self.require(draft, "review")
        if not self.config.images.enabled or not draft.data.get("post_text"):
            raise ValueError("Image generation is disabled or no LinkedIn text is selected.")
        draft = self.store.update(draft, status="generating_brief", changes={"notice": ""})
        try:
            brief = self.generator.generate_image_brief(
                draft.data["article_text"],
                draft.data["post_text"],
                instructions=draft.data.get("image_instructions") or "",
            )
        except Exception as exc:
            logger.warning("Visual brief failed (%s)", type(exc).__name__)
            return self.fail_generation(
                draft, "review", "Could not create an image concept. Retry or continue text-only."
            )
        return self.store.update(
            draft,
            status="review",
            changes={
                "brief": brief,
                "notice": "Review the image idea, then choose Generate image.",
            },
        )

    def generate_image(self, draft: Draft) -> Draft:
        self.require(draft, "review")
        if not self.config.images.enabled or not self.image_generator:
            raise ValueError("Image generation is disabled.")
        if not draft.data.get("brief"):
            raise ValueError("Create an image idea first.")
        count = draft.data.get("image_generations", 0)
        if count >= self.config.images.max_generations:
            raise ValueError(
                "Image generation limit reached for this draft. Keep the current image or publish text-only."
            )
        draft = self.store.update(
            draft, status="generating_image", changes={"image_generations": count + 1, "notice": ""}
        )
        path = self.image_dir / draft.id / f"{draft.revision}.png"
        try:
            data = self.image_generator.generate(draft.data["brief"]["prompt"])
            validate_image_bytes(data)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except Exception as exc:
            logger.warning("Image generation failed (%s)", type(exc).__name__)
            path.unlink(missing_ok=True)
            return self.fail_generation(
                draft,
                "review",
                "Image generation did not complete. This attempt counts toward the limit; retry explicitly or use text-only.",
            )
        try:
            result = self.store.update(
                draft,
                status="review",
                changes={
                    "image_path": str(path),
                    "image_sha256": hashlib.sha256(data).hexdigest(),
                    "image_for_text_hash": text_hash(draft.data["post_text"]),
                    "image_needs_review": False,
                    "image_alt_text": draft.data["brief"]["alt_text"],
                    "image_asset_urn": None,
                    "notice": "AI-generated image ready. Review it with the complete text before publishing.",
                },
            )
        except DraftConflict:
            path.unlink(missing_ok=True)
            raise
        self.telemetry.record(
            "image_generation", draft_id=draft.id, status="success", attempt=count + 1
        )
        return result

    def image_matches(self, draft: Draft) -> bool:
        return not draft.data.get("image_needs_review") and draft.data.get(
            "image_for_text_hash"
        ) == text_hash(draft.data.get("post_text", ""))

    def keep_image(self, draft: Draft) -> Draft:
        self.require(draft, "review")
        self.image_path(draft)
        return self.store.update(
            draft,
            changes={
                "image_for_text_hash": text_hash(draft.data["post_text"]),
                "image_needs_review": False,
                "notice": "Existing image kept. Review this version before publishing.",
            },
        )

    def remove_image(self, draft: Draft) -> Draft:
        self.require(draft, "review")
        return self.store.update(
            draft,
            changes={
                "image_path": None,
                "image_sha256": None,
                "image_asset_urn": None,
                "image_needs_review": False,
                "notice": "Text-only draft. Review and press Publish to continue.",
            },
        )

    def image_path(self, draft: Draft) -> Path:
        path = Path(draft.data.get("image_path") or "")
        if not path.resolve().is_relative_to(self.image_dir.resolve()) or not path.is_file():
            raise ValueError(
                "The reviewed image is missing. Regenerate it or remove it before publishing."
            )
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != draft.data.get("image_sha256"):
            raise ValueError("The saved image changed. Regenerate it before publishing.")
        validate_image_bytes(data)
        return path

    def cancel(self, draft: Draft) -> Draft:
        if draft.status == "publishing" or draft.status in TERMINAL_STATUSES:
            raise DraftConflict(
                "This operation cannot be cancelled now. Use /resume to see its status."
            )
        return self.store.update(draft, status="cancelled")

    def abort_edit(self, draft: Draft) -> Draft:
        self.require(draft, "editing_text", "editing_idea")
        return self.store.update(draft, status="review")

    def publish(self, draft: Draft) -> Draft:
        self.require(draft, "review")
        feed = draft.data["feed_type"]
        if any(
            draft.data.get(p + "_status") in {"published", "publishing", "uncertain"}
            for p in ("linkedin", "twitter")
        ):
            raise DraftConflict(
                "A publication attempt is already recorded. Review its status; do not resubmit."
            )
        image_path = None
        if feed in {"linkedin", "both"}:
            if not self.config.linkedin_token:
                raise ValueError("LINKEDIN_TOKEN is missing.")
            if not draft.data.get("post_text"):
                raise ValueError("No LinkedIn draft selected.")
            if reviewed_post(draft.data["post_text"]) != draft.data["post_text"]:
                raise ValueError("Text needs a fresh review before publishing.")
            if draft.data.get("image_path"):
                if not self.config.images.enabled:
                    raise ValueError("Images are disabled. Remove the image and review text-only.")
                if not self.image_matches(draft):
                    raise ValueError(
                        "Text or image instructions changed. Regenerate or explicitly keep the image first."
                    )
                image_path = self.image_path(draft)
        if feed in {"twitter", "both"} and (
            not self.config.x_credentials.is_configured or not draft.data.get("x_thread")
        ):
            raise ValueError("X credentials or reviewed thread are missing.")
        draft = self.store.update(
            draft, status="publishing", changes={"approved_revision": draft.revision, "notice": ""}
        )
        result = self.publisher.run(draft, image_path=image_path)
        self.telemetry.record(
            "draft_publish", draft_id=draft.id, status=result.status, with_image=bool(image_path)
        )
        return result
