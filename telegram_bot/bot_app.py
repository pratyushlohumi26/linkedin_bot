#!/usr/bin/env python3
"""Telegram bot application bootstrap."""

from __future__ import annotations

import logging

from telebot import TeleBot

from telegram_bot.config import AppConfig, load_config
from telegram_bot.handlers import register_handlers

logger = logging.getLogger(__name__)


def create_bot(config: AppConfig) -> TeleBot:
    bot = TeleBot(config.telegram_token)
    register_handlers(bot, config)
    return bot


def _start_polling(bot: TeleBot, config: AppConfig) -> None:
    logger.info("Starting Telegram bot in polling mode")
    bot.enable_save_next_step_handlers(delay=2)
    bot.load_next_step_handlers()
    bot.infinity_polling(skip_pending=config.telegram_drop_pending_updates)


def _start_webhook(bot: TeleBot, config: AppConfig) -> None:
    if not config.telegram_webhook_public_url:
        raise ValueError("TELEGRAM_WEBHOOK_PUBLIC_URL must be configured for webhook mode.")

    path = config.telegram_webhook_path
    webhook_url = f"{config.telegram_webhook_public_url}/{path}/"
    logger.info(
        "Starting Telegram bot in webhook mode on %s:%s at /%s/",
        config.telegram_webhook_listen,
        config.telegram_webhook_port,
        path,
    )

    bot.remove_webhook()
    bot.run_webhooks(
        listen=config.telegram_webhook_listen,
        port=config.telegram_webhook_port,
        url_path=path,
        webhook_url=webhook_url,
        drop_pending_updates=config.telegram_drop_pending_updates,
        secret_token=config.telegram_webhook_secret_token,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    bot = create_bot(config)

    if config.telegram_mode == "webhook":
        _start_webhook(bot, config)
    else:
        _start_polling(bot, config)


if __name__ == "__main__":
    main()
