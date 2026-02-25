#!/usr/bin/env python3
"""Inline keyboard markups for Telegram bot."""

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

def feed_type_selection():
    """Buttons to select Twitter, LinkedIn, or Both."""
    markup = InlineKeyboardMarkup()
    markup.row_width = 3
    markup.add(
        InlineKeyboardButton("Twitter Thread", callback_data="twitter"),
        InlineKeyboardButton("Linkedin Post", callback_data="linkedin"),
        InlineKeyboardButton("Both", callback_data="both"),
    )
    return markup

def confirmation_selection():
    """Buttons to confirm Yes or No."""
    markup = InlineKeyboardMarkup()
    markup.row_width = 2
    markup.add(
        InlineKeyboardButton("Yes", callback_data="yes"),
        InlineKeyboardButton("No", callback_data="no"),
    )
    return markup