#!/usr/bin/env python3
"""Inline keyboard markups for Telegram bot."""

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup


def feed_type_selection() -> InlineKeyboardMarkup:
    """Buttons to select Twitter, LinkedIn, or Both."""
    markup = InlineKeyboardMarkup()
    markup.row_width = 3
    markup.add(
        InlineKeyboardButton("Twitter Thread", callback_data="twitter"),
        InlineKeyboardButton("LinkedIn Post", callback_data="linkedin"),
        InlineKeyboardButton("Both", callback_data="both"),
    )
    return markup


def confirmation_selection() -> InlineKeyboardMarkup:
    """Buttons to confirm Yes or No."""
    markup = InlineKeyboardMarkup()
    markup.row_width = 2
    markup.add(
        InlineKeyboardButton("Yes", callback_data="yes"),
        InlineKeyboardButton("No", callback_data="no"),
    )
    return markup


def linkedin_variant_selection() -> InlineKeyboardMarkup:
    """Buttons to choose among LinkedIn variants A/B/C, regenerate, or cancel."""
    markup = InlineKeyboardMarkup()
    markup.row_width = 3
    markup.add(
        InlineKeyboardButton("Post A", callback_data="linkedin_variant_a"),
        InlineKeyboardButton("Post B", callback_data="linkedin_variant_b"),
        InlineKeyboardButton("Post C", callback_data="linkedin_variant_c"),
    )
    markup.row_width = 2
    markup.add(
        InlineKeyboardButton("Regenerate", callback_data="linkedin_variant_regen"),
        InlineKeyboardButton("Cancel", callback_data="linkedin_variant_cancel"),
    )
    return markup
