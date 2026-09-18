"""Revision-bound inline controls for review-first publishing."""

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_bot.drafts import Draft


def feed_type_selection() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        *(
            InlineKeyboardButton(label, callback_data=f"dest:{value}")
            for label, value in (
                ("LinkedIn", "linkedin"),
                ("X thread", "twitter"),
                ("Both", "both"),
            )
        )
    )
    return markup


def draft_keyboard(draft: Draft, rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    for row in rows:
        markup.row(
            *(
                InlineKeyboardButton(label, callback_data=f"d:{draft.id}:{draft.revision}:{action}")
                for label, action in row
            )
        )
    return markup
