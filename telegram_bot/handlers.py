#!/usr/bin/env python3
"""Telegram handlers registration."""

import time
import logging

from telebot.types import Message, CallbackQuery

from bot_app import bot, user_info
from keyboards import feed_type_selection, confirmation_selection
from scraper import extract_text_from_url
from summarizer import summarize_with_gpt4_linkedin, call_gpt4_x
from linkedin_client import LinkedinAutomate
from twitter_client import post_twitter

logger = logging.getLogger(__name__)

@bot.message_handler(commands=['help', 'start'])
def send_welcome(message: Message):
    bot.send_message(
        message.chat.id,
        "/help and /start to get the help\n/start_post to post in social Media",
    )

@bot.message_handler(commands=['start_post'])
def linkedin_post_handler(message: Message):
    bot.send_message(
        message.chat.id,
        "Select which type of Post you want to make",
        reply_markup=feed_type_selection(),
    )

@bot.message_handler(func=lambda message: True)
def echo_message(message: Message):
    bot.reply_to(message, message.text)

def process_text_post(message: Message):
    text = message.text
    blog_text = extract_text_from_url(text)
    user_info["description"] = blog_text

    bot.send_message(
        message.chat.id,
        f"This is what the scrapped text looks like ::\n<b>{blog_text[:100]}</b>",
        reply_markup=confirmation_selection(),
        parse_mode="html",
    )

@bot.callback_query_handler(lambda query: query.data in ["twitter", "linkedin", "both"])
def post_type_callback_handler(call: CallbackQuery):
    if call.data == "linkedin":
        user_info["feed_type"] = 'linkedin'
    elif call.data == "twitter":
        user_info["feed_type"] = 'twitter'
    elif call.data == "both":
        user_info["feed_type"] = 'both'
    else:
        bot.send_message(call.message, "Something went wrong")

    msg = bot.reply_to(call.message, 'Whats the link which needs to be posted?')
    bot.register_next_step_handler(msg, process_text_post)

@bot.callback_query_handler(lambda query: query.data in ["yes", "no"])
def confirmation_callback_handler(call: CallbackQuery):
    if call.data == "yes":
        description = user_info.get("description", "")
        post_type = user_info.get("feed_type", "")

        if post_type == "linkedin":
            summary = summarize_with_gpt4_linkedin(description)
            bot.reply_to(call.message, f"Posting to LinkedIn...\n{summary}")
            response = LinkedinAutomate(description=summary).main_func()
            logger.info("LinkedIn response: %s", response)
            if response and getattr(response, "status_code", None) == 201:
                post_url = "https://www.linkedin.com/in/pratyush-lohumi/recent-activity/all/"
                bot.reply_to(call.message, f"LinkedIn post successful!\n{post_url}")

        elif post_type == "twitter":
            thread = call_gpt4_x(description)
            time.sleep(3)
            link = post_twitter(tweet_thread=thread)
            logger.info("Twitter thread link: %s", link)
            bot.reply_to(call.message, f"Twitter thread posted:\n{link}")

        elif post_type == "both":
            thread = call_gpt4_x(description)
            summary = summarize_with_gpt4_linkedin(description)
            linkedin_resp = LinkedinAutomate(description=summary).main_func()
            twitter_link = post_twitter(tweet_thread=thread)
            logger.info("LinkedIn & Twitter responses: %s, %s", linkedin_resp, twitter_link)
            bot.reply_to(call.message, f"LinkedIn:\n{linkedin_resp}\nTwitter:\n{twitter_link}")
    elif call.data == "no":
        retry = bot.send_message(call.message.chat.id, "No problem, send a new link:")
        bot.register_next_step_handler(retry, process_text_post)