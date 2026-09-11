#!/usr/bin/env python3
"""Telegram handlers registration."""

from __future__ import annotations

import logging
from typing import Any

from telebot import TeleBot
from telebot.types import CallbackQuery, Message

from telegram_bot.config import AppConfig
from telegram_bot.keyboards import confirmation_selection, feed_type_selection
from telegram_bot.linkedin_client import LinkedinAutomate
from telegram_bot.scraper import extract_text_from_url
from telegram_bot.summarizer import ContentGenerator
from telegram_bot.twitter_client import TwitterPublisher

logger = logging.getLogger(__name__)


def register_handlers(bot: TeleBot, config: AppConfig) -> None:
    """Register all Telegram handlers on the provided bot instance."""

    sessions: dict[int, dict[str, Any]] = {}
    generator = ContentGenerator(config.llm)
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
                message.chat.id, "Please send a valid URL starting with http:// or https://"
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

        msg = bot.send_message(chat_id, "Send the article URL you want to convert into a post:")
        bot.register_next_step_handler(msg, process_text_post)

    @bot.callback_query_handler(func=lambda query: query.data in ["yes", "no"])
    def confirmation_callback_handler(call: CallbackQuery) -> None:
        chat_id = call.message.chat.id
        user_id = call.from_user.id if call.from_user else None
        if not ensure_allowed(user_id, chat_id):
            return

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

        if feed_type == "linkedin":
            _publish_linkedin(chat_id, description)
        elif feed_type == "twitter":
            _publish_twitter(chat_id, description)
        elif feed_type == "both":
            _publish_both(chat_id, description)
        else:
            bot.send_message(chat_id, "Unknown post type. Please run /start_post again.")

    def _publish_linkedin(chat_id: int, description: str) -> None:
        if not config.linkedin_token:
            bot.send_message(chat_id, "LINKEDIN_TOKEN is missing. Add it to your .env and retry.")
            return

        try:
            summary = generator.generate_linkedin_post(description)
            bot.send_message(chat_id, f"Posting to LinkedIn...\n\n{summary}")
            response = LinkedinAutomate(access_token=config.linkedin_token).publish_post(summary)
            if response and response.status_code == 201:
                bot.send_message(chat_id, "LinkedIn post published successfully.")
            else:
                bot.send_message(chat_id, "LinkedIn post failed. Check token permissions and logs.")
        except Exception as err:
            logger.exception("LinkedIn publishing failed: %s", err)
            bot.send_message(chat_id, f"LinkedIn flow failed: {err}")

    def _publish_twitter(chat_id: int, description: str) -> None:
        try:
            thread = generator.generate_x_thread(description)
            link = get_twitter_publisher().post_thread(thread)
            bot.send_message(chat_id, f"X thread posted:\n{link}")
        except Exception as err:
            logger.exception("X publishing failed: %s", err)
            bot.send_message(chat_id, f"X flow failed: {err}")

    def _publish_both(chat_id: int, description: str) -> None:
        linkedin_status = "Not attempted"
        twitter_status = "Not attempted"

        if config.linkedin_token:
            try:
                summary = generator.generate_linkedin_post(description)
                response = LinkedinAutomate(access_token=config.linkedin_token).publish_post(
                    summary
                )
                linkedin_status = (
                    "Success" if response and response.status_code == 201 else "Failed"
                )
            except Exception as err:
                logger.exception("LinkedIn publish in dual mode failed: %s", err)
                linkedin_status = f"Failed: {err}"
        else:
            linkedin_status = "Skipped (missing LINKEDIN_TOKEN)"

        try:
            thread = generator.generate_x_thread(description)
            link = get_twitter_publisher().post_thread(thread)
            twitter_status = f"Success ({link})"
        except Exception as err:
            logger.exception("X publish in dual mode failed: %s", err)
            twitter_status = f"Failed: {err}"

        bot.send_message(
            chat_id,
            f"LinkedIn: {linkedin_status}\nX: {twitter_status}",
        )
