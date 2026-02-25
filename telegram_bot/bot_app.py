#!/usr/bin/env python3
"""Telegram bot for AI-powered LinkedIn and X/Twitter posting."""

# Standard library
import os
import logging
from handlers import *
from api_key import openai_key, TELEGRAM_TOKEN

# set OpenAI key for any underlying libraries that may look at env
os.environ['OPENAI_API_KEY'] = openai_key

# configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

from telebot import TeleBot

# create bot instance and shared state
bot = TeleBot(TELEGRAM_TOKEN)
user_info: dict = {}

# import modules that register handlers and provide functionality
import handlers

def main():
    """Enable handler persistence and start Telegram bot polling."""
    bot.enable_save_next_step_handlers(delay=2)
    bot.load_next_step_handlers()
    logger.info("Starting Telegram bot polling...")
    bot.infinity_polling()

if __name__ == "__main__":
    main()