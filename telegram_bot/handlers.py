#!/usr/bin/env python3
"""Telegram handlers registration."""

from __future__ import annotations

import logging
import time
from typing import Any

from telebot import TeleBot
from telebot.types import CallbackQuery, Message

from telegram_bot.config import AppConfig
from telegram_bot.keyboards import (
    confirmation_selection,
    feed_type_selection,
    linkedin_variant_selection,
)
from telegram_bot.linkedin_client import LinkedinAutomate
from telegram_bot.research_agent import ResearchAgent, build_research_topic
from telegram_bot.scraper import extract_text_from_url
from telegram_bot.summarizer import ContentGenerator
from telegram_bot.telemetry import TelemetryLogger
from telegram_bot.twitter_client import TwitterPublisher

logger = logging.getLogger(__name__)
_LINKEDIN_VARIANT_CALLBACKS = {
    "linkedin_variant_a",
    "linkedin_variant_b",
    "linkedin_variant_c",
    "linkedin_variant_regen",
    "linkedin_variant_cancel",
}


def register_handlers(bot: TeleBot, config: AppConfig) -> None:
    """Register all Telegram handlers on the provided bot instance."""

    sessions: dict[int, dict[str, Any]] = {}
    generator = ContentGenerator(config.llm)
    telemetry = TelemetryLogger(config.telemetry_log_path)
    research_agent = ResearchAgent(
        enabled=config.enable_research_agent,
        provider=config.search_provider,
        api_key=config.search_api_key,
        max_links=config.search_max_links,
    )
    twitter_publisher: TwitterPublisher | None = None

    def is_allowed(user_id: int | None) -> bool:
        if not config.allowed_user_ids:
            return True
        return user_id in config.allowed_user_ids

    def ensure_allowed(user_id: int | None, chat_id: int) -> bool:
        if is_allowed(user_id):
            return True
        bot.send_message(chat_id, "You are not allowed to use this bot.")
        return False

    def get_session(chat_id: int) -> dict[str, Any]:
        return sessions.setdefault(chat_id, {})

    def get_twitter_publisher() -> TwitterPublisher:
        nonlocal twitter_publisher
        if twitter_publisher is None:
            twitter_publisher = TwitterPublisher(config.x_credentials)
        return twitter_publisher

    def _clear_variant_state(session: dict[str, Any]) -> None:
        session.pop("linkedin_variants", None)

    def _truncate_preview(text: str, *, limit: int = 650) -> str:
        compact = text.strip()
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3].rstrip() + "..."

    def _generate_linkedin_variants(
        chat_id: int, session: dict[str, Any], *, regenerate: bool
    ) -> None:
        description = session.get("description")
        if not description:
            bot.send_message(chat_id, "No article content found. Start again with /start_post.")
            return

        try:
            variants = generator.generate_linkedin_variants(
                description,
                core_hashtags=config.linkedin_hashtag_core,
                secondary_hashtags=config.linkedin_hashtag_secondary,
            )
        except Exception as err:
            logger.exception("LinkedIn variant generation failed: %s", err)
            bot.send_message(chat_id, f"LinkedIn draft generation failed: {err}")
            telemetry.record("linkedin_variant_generation", status="failed", reason=str(err))
            return

        session["linkedin_variants"] = variants

        telemetry.record(
            "linkedin_variant_generation",
            status="success",
            regenerate=regenerate,
            variant_lengths={key: len(value) for key, value in variants.items()},
        )

        bot.send_message(
            chat_id,
            "Generated 3 LinkedIn variants. Review and choose one to publish:",
        )
        for key in ("A", "B", "C"):
            preview = _truncate_preview(variants[key])
            bot.send_message(chat_id, f"Variant {key}:\n\n{preview}")

        bot.send_message(
            chat_id,
            "Pick A/B/C, regenerate, or cancel:",
            reply_markup=linkedin_variant_selection(),
        )

    def _post_first_comment_after_publish(
        *,
        chat_id: int,
        linkedin_client: LinkedinAutomate,
        post_urn: str,
        article_text: str,
        linkedin_post_text: str,
    ) -> str:
        if not config.linkedin_enable_first_comment:
            return "skipped (disabled by config)"

        if not post_urn:
            return "skipped (missing LinkedIn post urn)"

        references: list[dict[str, str]] = []
        if research_agent.is_ready:
            references = research_agent.gather_references(topic=build_research_topic(article_text))

        try:
            comment_text = generator.generate_linkedin_first_comment(
                linkedin_post=linkedin_post_text,
                article_excerpt=article_text,
                references=references,
            )
        except Exception as err:
            logger.exception("First-comment generation failed: %s", err)
            telemetry.record("linkedin_first_comment", status="failed", reason=str(err))
            return f"failed to generate comment ({err})"

        if config.linkedin_first_comment_delay_seconds > 0:
            time.sleep(config.linkedin_first_comment_delay_seconds)

        response = linkedin_client.post_comment(post_urn=post_urn, comment_text=comment_text)
        if response is None:
            telemetry.record("linkedin_first_comment", status="failed", reason="api_error")
            return "failed to publish comment"

        telemetry.record(
            "linkedin_first_comment",
            status="success",
            post_urn=post_urn,
            used_research_links=bool(references),
            links_count=len(references),
        )
        return "posted"

    def _publish_linkedin(
        chat_id: int,
        *,
        post_text: str,
        article_text: str,
        variant_key: str,
    ) -> str:
        if not config.linkedin_token:
            bot.send_message(chat_id, "LINKEDIN_TOKEN is missing. Add it to your .env and retry.")
            return "Skipped (missing LINKEDIN_TOKEN)"

        try:
            bot.send_message(chat_id, f"Posting Variant {variant_key} to LinkedIn...")
            linkedin_client = LinkedinAutomate(access_token=config.linkedin_token)
            result = linkedin_client.publish_post(post_text)
            if not result or result.status_code != 201:
                telemetry.record("linkedin_publish", status="failed", variant=variant_key)
                bot.send_message(chat_id, "LinkedIn post failed. Check token permissions and logs.")
                return "Failed"

            first_comment_status = _post_first_comment_after_publish(
                chat_id=chat_id,
                linkedin_client=linkedin_client,
                post_urn=result.post_urn or "",
                article_text=article_text,
                linkedin_post_text=post_text,
            )
            telemetry.record(
                "linkedin_publish",
                status="success",
                variant=variant_key,
                post_urn=result.post_urn,
                first_comment_status=first_comment_status,
            )

            if config.linkedin_enable_first_comment:
                bot.send_message(
                    chat_id,
                    f"LinkedIn post published successfully. First comment: {first_comment_status}.",
                )
            else:
                bot.send_message(chat_id, "LinkedIn post published successfully.")
            return "Success"
        except Exception as err:
            logger.exception("LinkedIn publishing failed: %s", err)
            telemetry.record(
                "linkedin_publish", status="failed", variant=variant_key, reason=str(err)
            )
            bot.send_message(chat_id, f"LinkedIn flow failed: {err}")
            return f"Failed: {err}"

    def _publish_twitter(chat_id: int, description: str, *, send_message: bool = True) -> str:
        try:
            thread = generator.generate_x_thread(description)
            link = get_twitter_publisher().post_thread(thread)
            telemetry.record("twitter_publish", status="success")
            if send_message:
                bot.send_message(chat_id, f"X thread posted:\n{link}")
            return f"Success ({link})"
        except Exception as err:
            logger.exception("X publishing failed: %s", err)
            telemetry.record("twitter_publish", status="failed", reason=str(err))
            if send_message:
                bot.send_message(chat_id, f"X flow failed: {err}")
            return f"Failed: {err}"

    @bot.message_handler(commands=["help", "start"])
    def send_welcome(message: Message) -> None:
        if not ensure_allowed(message.from_user.id if message.from_user else None, message.chat.id):
            return
        bot.send_message(
            message.chat.id,
            "Use /start_post to generate and post to LinkedIn, X, or both.",
        )

    @bot.message_handler(commands=["start_post"])
    def start_post_flow(message: Message) -> None:
        if not ensure_allowed(message.from_user.id if message.from_user else None, message.chat.id):
            return
        bot.send_message(
            message.chat.id,
            "Select where to publish:",
            reply_markup=feed_type_selection(),
        )

    def process_text_post(message: Message) -> None:
        if not ensure_allowed(message.from_user.id if message.from_user else None, message.chat.id):
            return

        url = (message.text or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            bot.send_message(
                message.chat.id,
                "Please send a valid URL starting with http:// or https://",
            )
            retry = bot.send_message(message.chat.id, "Send the article URL again:")
            bot.register_next_step_handler(retry, process_text_post)
            return

        blog_text = extract_text_from_url(url, timeout_seconds=config.scraper_timeout_seconds)
        if not blog_text:
            retry = bot.send_message(
                message.chat.id,
                "Could not scrape this URL. Please send another blog/article link.",
            )
            bot.register_next_step_handler(retry, process_text_post)
            return

        session = get_session(message.chat.id)
        session["description"] = blog_text
        _clear_variant_state(session)

        preview = blog_text[:500]
        bot.send_message(
            message.chat.id,
            f"Scraped preview:\n\n<pre>{preview}</pre>",
            reply_markup=confirmation_selection(),
            parse_mode="html",
        )

    @bot.callback_query_handler(func=lambda query: query.data in ["twitter", "linkedin", "both"])
    def post_type_callback_handler(call: CallbackQuery) -> None:
        chat_id = call.message.chat.id
        user_id = call.from_user.id if call.from_user else None
        if not ensure_allowed(user_id, chat_id):
            return

        session = get_session(chat_id)
        session["feed_type"] = call.data

        bot.answer_callback_query(call.id)
        msg = bot.send_message(chat_id, "Send the article URL you want to convert into a post:")
        bot.register_next_step_handler(msg, process_text_post)

    @bot.callback_query_handler(func=lambda query: query.data in ["yes", "no"])
    def confirmation_callback_handler(call: CallbackQuery) -> None:
        chat_id = call.message.chat.id
        user_id = call.from_user.id if call.from_user else None
        if not ensure_allowed(user_id, chat_id):
            return

        bot.answer_callback_query(call.id)

        if call.data == "no":
            retry = bot.send_message(chat_id, "No worries. Send another URL:")
            bot.register_next_step_handler(retry, process_text_post)
            return

        session = get_session(chat_id)
        description = session.get("description")
        feed_type = session.get("feed_type")

        if not description or not feed_type:
            bot.send_message(chat_id, "Please start again with /start_post")
            return

        if feed_type == "twitter":
            _publish_twitter(chat_id, description)
            return

        _generate_linkedin_variants(chat_id, session, regenerate=False)

    @bot.callback_query_handler(func=lambda query: query.data in _LINKEDIN_VARIANT_CALLBACKS)
    def linkedin_variant_callback_handler(call: CallbackQuery) -> None:
        chat_id = call.message.chat.id
        user_id = call.from_user.id if call.from_user else None
        if not ensure_allowed(user_id, chat_id):
            return

        bot.answer_callback_query(call.id)

        session = get_session(chat_id)
        description = session.get("description")
        feed_type = session.get("feed_type")
        variants: dict[str, str] = session.get("linkedin_variants", {})

        if call.data == "linkedin_variant_cancel":
            _clear_variant_state(session)
            bot.send_message(chat_id, "Cancelled. Run /start_post to begin again.")
            return

        if call.data == "linkedin_variant_regen":
            _generate_linkedin_variants(chat_id, session, regenerate=True)
            return

        if not description or feed_type not in {"linkedin", "both"}:
            bot.send_message(chat_id, "Session expired. Please run /start_post again.")
            return

        selected_key = call.data.rsplit("_", 1)[-1].upper()
        selected_post = variants.get(selected_key)
        if not selected_post:
            bot.send_message(
                chat_id, "Could not find that variant. Please regenerate and choose again."
            )
            return

        linkedin_status = _publish_linkedin(
            chat_id,
            post_text=selected_post,
            article_text=description,
            variant_key=selected_key,
        )

        if feed_type == "both":
            twitter_status = _publish_twitter(chat_id, description, send_message=False)
            bot.send_message(chat_id, f"LinkedIn: {linkedin_status}\nX: {twitter_status}")

        _clear_variant_state(session)
