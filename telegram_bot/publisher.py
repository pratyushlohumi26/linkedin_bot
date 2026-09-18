"""Publish approved drafts once and persist each destination's result."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from telegram_bot.drafts import Draft, DraftStore
from telegram_bot.linkedin_client import build_linkedin_post_url
from telegram_bot.research_agent import ResearchAgent, build_research_topic
from telegram_bot.twitter_client import TwitterPublisher

logger = logging.getLogger(__name__)


class DraftPublisher:
    def __init__(self, config, store: DraftStore, generator, linkedin, twitter=None):
        self.config = config
        self.store = store
        self.generator = generator
        self.linkedin = linkedin
        self.twitter = twitter

    def _comment(self, draft: Draft) -> str:
        if not self.config.linkedin_enable_first_comment:
            return "disabled"
        if not draft.data.get("linkedin_urn"):
            return "skipped: post identifier unavailable"
        try:
            research = ResearchAgent(
                enabled=self.config.enable_research_agent,
                provider=self.config.search_provider,
                api_key=self.config.search_api_key,
                max_links=self.config.search_max_links,
            )
            references = (
                research.gather_references(topic=build_research_topic(draft.data["article_text"]))
                if research.is_ready
                else []
            )
            comment = self.generator.generate_linkedin_first_comment(
                linkedin_post=draft.data["post_text"],
                article_excerpt=draft.data["article_text"],
                references=references,
            )
            comment += "\n\nCreated with an AI agent (OpenHands) on behalf of the author."
            if self.config.linkedin_first_comment_delay_seconds:
                time.sleep(self.config.linkedin_first_comment_delay_seconds)
            response = self.linkedin.post_comment(
                post_urn=draft.data["linkedin_urn"], comment_text=comment
            )
            return "posted" if response is not None else "failed (main post is published)"
        except Exception as exc:
            logger.warning("First comment failed after publication (%s)", type(exc).__name__)
            return "failed (main post is published)"

    def run(self, draft: Draft, *, image_path: Path | None = None) -> Draft:
        feed = draft.data["feed_type"]
        if feed in {"linkedin", "both"}:
            if image_path and not draft.data.get("image_asset_urn"):
                try:
                    asset = self.linkedin.upload_image(
                        image_path, alt_text=draft.data.get("image_alt_text", "")
                    )
                except Exception as exc:
                    logger.warning("Image upload failed (%s)", type(exc).__name__)
                    return self.store.update(
                        draft,
                        status="review",
                        changes={
                            "notice": "Image upload failed before post creation. Nothing was published; retry or remove the image."
                        },
                    )
                draft = self.store.update(draft, changes={"image_asset_urn": asset})
            draft = self.store.update(draft, changes={"linkedin_status": "publishing"})
            try:
                result = self.linkedin.publish_post(
                    draft.data["post_text"],
                    image_asset_urn=draft.data.get("image_asset_urn"),
                    image_alt_text=draft.data.get("image_alt_text", ""),
                )
            except Exception as exc:
                logger.warning("LinkedIn publication outcome uncertain (%s)", type(exc).__name__)
                return self.store.update(
                    draft,
                    status="uncertain",
                    changes={
                        "linkedin_status": "uncertain",
                        "notice": "LinkedIn may have accepted the post. Check your profile before starting another post; automatic retry is blocked.",
                    },
                )
            if result is None or result.status_code != 201:
                return self.store.update(
                    draft,
                    status="review",
                    changes={
                        "linkedin_status": "failed",
                        "notice": "LinkedIn rejected the post. Check permissions or content; no automatic retry.",
                    },
                )
            draft = self.store.update(
                draft,
                changes={
                    "linkedin_status": "published",
                    "linkedin_urn": result.post_urn,
                    "linkedin_url": build_linkedin_post_url(result.post_urn),
                },
            )
        if feed in {"twitter", "both"}:
            draft = self.store.update(draft, changes={"twitter_status": "publishing"})
            try:
                twitter = self.twitter or TwitterPublisher(self.config.x_credentials)
                link = twitter.post_prepared_thread(draft.data["x_thread"])
            except Exception as exc:
                logger.warning("X thread outcome uncertain (%s)", type(exc).__name__)
                return self.store.update(
                    draft,
                    status="partial" if feed == "both" else "uncertain",
                    changes={
                        "twitter_status": "uncertain",
                        "notice": "X may contain a partial thread. Check X before retrying; any successful LinkedIn post is preserved.",
                    },
                )
            draft = self.store.update(
                draft, changes={"twitter_status": "published", "twitter_url": link}
            )
        draft = self.store.update(draft, status="published")
        if draft.data.get("linkedin_status") == "published":
            draft = self.store.update(draft, changes={"first_comment_status": self._comment(draft)})
        return draft
