"""Telegram review UI backed by persistent, revision-bound drafts."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

from telebot import TeleBot, util
from telebot.types import CallbackQuery, Message

from telegram_bot.config import AppConfig
from telegram_bot.drafts import BUSY_STATUSES, TERMINAL_STATUSES, Draft, DraftConflict
from telegram_bot.keyboards import draft_keyboard, feed_type_selection
from telegram_bot.workflow import DraftWorkflow

logger = logging.getLogger(__name__)


class DraftBotController:
    def __init__(self, bot: TeleBot, config: AppConfig, *, workflow: DraftWorkflow | None = None):
        self.bot = bot
        self.config = config
        self.flow = workflow or DraftWorkflow(config)
        self.store = self.flow.store
        self.store.recover_interrupted()
        self.flow.cleanup()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="draft-job")
        self.slots = BoundedSemaphore(2)
        bot.register_message_handler(self.start, commands=["start", "help", "start_post"])
        bot.register_message_handler(self.resume, commands=["resume"])
        bot.register_message_handler(self.cancel, commands=["cancel"])
        bot.register_message_handler(self.receive_text, content_types=["text"])
        bot.register_callback_query_handler(self.callback, func=lambda call: True)

    def close(self):
        self.executor.shutdown(wait=True)

    def allowed(self, user_id: int | None, chat_id: int) -> bool:
        if user_id is not None and (
            not self.config.allowed_user_ids or user_id in self.config.allowed_user_ids
        ):
            return True
        self.bot.send_message(chat_id, "You are not allowed to use this bot.")
        return False

    def start(self, message: Message):
        if not self.allowed(message.from_user.id if message.from_user else None, message.chat.id):
            return
        if util.extract_command(message.text or "") != "start_post":
            self.bot.send_message(
                message.chat.id,
                "AI-assisted social drafts: /start_post to begin, /resume to review your saved draft, /cancel to discard it. Nothing publishes until you approve the final preview.",
            )
            return
        self.flow.cleanup()
        self.bot.send_message(
            message.chat.id,
            "Choose where to publish. Images are supported on LinkedIn; X uses a reviewed text thread.",
            reply_markup=feed_type_selection(),
        )

    def resume(self, message: Message):
        if not self.allowed(message.from_user.id if message.from_user else None, message.chat.id):
            return
        draft = self.store.latest(message.chat.id, message.from_user.id)
        if draft:
            self.safe_present(draft)
        else:
            self.bot.send_message(message.chat.id, "No saved draft. Use /start_post.")

    def cancel(self, message: Message):
        if not self.allowed(message.from_user.id if message.from_user else None, message.chat.id):
            return
        draft = self.store.latest(message.chat.id, message.from_user.id)
        if not draft:
            self.bot.send_message(message.chat.id, "No active draft.")
            return
        try:
            self.safe_present(self.flow.cancel(draft))
        except (ValueError, DraftConflict) as exc:
            self.bot.send_message(message.chat.id, str(exc))

    def receive_text(self, message: Message):
        if not self.allowed(message.from_user.id if message.from_user else None, message.chat.id):
            return
        draft = self.store.latest(message.chat.id, message.from_user.id)
        if not draft:
            self.bot.send_message(message.chat.id, "Use /start_post to begin.")
            return
        try:
            if draft.status == "awaiting_url":
                self.submit(draft, lambda: self.flow.scrape(draft, (message.text or "").strip()))
            elif draft.status in {"editing_text", "editing_idea"}:
                self.safe_present(self.flow.edit(draft, message.text or ""))
            else:
                self.bot.send_message(
                    message.chat.id,
                    "Use the draft buttons or /resume. To replace text, choose Edit text first.",
                )
        except (ValueError, DraftConflict) as exc:
            self.bot.send_message(message.chat.id, str(exc))

    def submit(self, draft: Draft, operation):
        if not self.slots.acquire(blocking=False):
            self.bot.send_message(
                draft.chat_id,
                "The bot is busy with two operations. Please retry shortly; your draft is saved.",
            )
            return

        def work():
            try:
                result = operation()
                self.safe_present(result)
            except (ValueError, DraftConflict) as exc:
                self.bot.send_message(draft.chat_id, str(exc))
            except Exception as exc:
                logger.warning("Draft operation failed (%s)", type(exc).__name__)
                current = self.store.get(draft.id, draft.chat_id, draft.user_id)
                if current.status in BUSY_STATUSES:
                    if current.status == "publishing":
                        changes = {
                            p + "_status": "uncertain"
                            for p in ("linkedin", "twitter")
                            if current.data.get(p + "_status") == "publishing"
                        }
                        self.store.update(
                            current,
                            status="uncertain",
                            changes={
                                **changes,
                                "notice": "Operation interrupted. Check publication status before retrying.",
                            },
                        )
                    else:
                        status = (
                            "review"
                            if current.data.get("post_text")
                            else "variants" if current.data.get("variants") else "awaiting_url"
                        )
                        self.store.update(
                            current,
                            status=status,
                            changes={
                                "notice": "Operation failed. Use /resume; no automatic retry."
                            },
                        )
                self.bot.send_message(
                    draft.chat_id,
                    "Operation interrupted. Use /resume to inspect the saved result before trying again.",
                )
            finally:
                self.slots.release()

        try:
            self.bot.send_message(
                draft.chat_id,
                "Working on your draft… /resume shows progress. Generation does not publish anything.",
            )
            self.executor.submit(work)
        except Exception:
            self.slots.release()
            raise

    def callback(self, call: CallbackQuery):
        if not call.message:
            return
        chat_id = call.message.chat.id
        user_id = call.from_user.id if call.from_user else None
        if not self.allowed(user_id, chat_id):
            return
        self.bot.answer_callback_query(call.id)
        try:
            data = call.data or ""
            if data.startswith("dest:"):
                self.safe_present(self.store.create(chat_id, user_id, data.split(":", 1)[1]))
                return
            if not data.startswith("d:"):
                raise DraftConflict(
                    "These buttons belong to the previous bot flow. Use /start_post."
                )
            _, draft_id, revision, action = data.split(":", 3)
            draft = self.store.get(draft_id, chat_id, user_id)
            if draft.revision != int(revision):
                raise DraftConflict("This preview is outdated. Use /resume for the latest version.")
            if action == "cancel":
                result = self.flow.cancel(draft)
            elif action == "back":
                result = self.flow.abort_edit(draft)
            elif action == "source":
                self.flow.require(draft, "source_review")
                result = self.store.update(draft, status="awaiting_url")
            elif action in {"variants", "brief", "generate", "publish"}:
                operations = {
                    "variants": self.flow.generate_variants,
                    "brief": self.flow.make_brief,
                    "generate": self.flow.generate_image,
                    "publish": self.flow.publish,
                }
                self.submit(draft, lambda: operations[action](draft))
                return
            elif action in {"A", "B", "C"}:
                result = self.flow.select(draft, action)
            elif action in {"edit_text", "edit_idea"}:
                result = self.flow.request_edit(draft, action.removeprefix("edit_"))
            elif action == "remove":
                result = self.flow.remove_image(draft)
            elif action == "keep":
                result = self.flow.keep_image(draft)
            elif action == "original":
                self.flow.require(draft, "review")
                with self.flow.image_path(draft).open("rb") as image:
                    self.bot.send_document(
                        chat_id,
                        image,
                        caption=f"AI-generated original — draft {draft.id}, version {draft.revision}",
                    )
                return
            else:
                raise ValueError("Unknown action. Use /resume.")
            self.safe_present(result)
        except (ValueError, DraftConflict) as exc:
            self.bot.send_message(chat_id, str(exc))
        except Exception as exc:
            logger.warning("Telegram draft action failed (%s)", type(exc).__name__)
            self.bot.send_message(
                chat_id, "Could not complete this action. Your draft is saved; use /resume."
            )

    def safe_present(self, draft: Draft):
        try:
            current = self.store.get(draft.id, draft.chat_id, draft.user_id)
            if current.revision != draft.revision:
                return
            self.present(draft)
        except Exception as exc:
            logger.warning("Draft preview delivery failed (%s)", type(exc).__name__)
            try:
                self.bot.send_message(
                    draft.chat_id,
                    "Could not display the complete preview. Nothing new was published by this message. Use /resume to check the saved draft/result.",
                )
            except Exception:
                logger.warning("Telegram unavailable; draft/result remains persisted")

    def present(self, draft: Draft):
        chat = draft.chat_id
        title = f"Draft {draft.id} — version {draft.revision}"
        if draft.data.get("notice"):
            self.bot.send_message(chat, draft.data["notice"])
        if draft.status in TERMINAL_STATUSES:
            lines = [title, f"Status: {draft.status}"]
            if draft.data.get("linkedin_status") == "published":
                lines.append("LinkedIn post published successfully.")
                lines.append(
                    draft.data.get("linkedin_url")
                    or "LinkedIn did not return a post URL. Do not republish."
                )
            elif draft.data.get("linkedin_status"):
                lines.append("LinkedIn: " + draft.data["linkedin_status"])
            if draft.data.get("twitter_status"):
                lines.append("X: " + draft.data["twitter_status"])
                if draft.data.get("twitter_url"):
                    lines.append(draft.data["twitter_url"])
            if draft.data.get("first_comment_status"):
                lines.append("First comment: " + draft.data["first_comment_status"])
            self.bot.send_message(chat, "\n".join(lines))
            return
        if draft.status in BUSY_STATUSES:
            rows = [] if draft.status == "publishing" else [[("Cancel draft", "cancel")]]
            self.bot.send_message(
                chat,
                title + "\nStatus: " + draft.status + "\nUse /resume to check again.",
                reply_markup=draft_keyboard(draft, rows),
            )
            return
        if draft.status == "awaiting_url":
            self.bot.send_message(chat, title + "\nSend the public article URL.")
            return
        if draft.status == "source_review":
            self.bot.send_message(
                chat,
                title + "\nScraped excerpt:\n\n" + draft.data["article_text"][:700],
                reply_markup=draft_keyboard(
                    draft,
                    [
                        [("Generate drafts", "variants"), ("Different URL", "source")],
                        [("Cancel", "cancel")],
                    ],
                ),
            )
            return
        if draft.status == "variants":
            for key, text in draft.data["variants"].items():
                preview = text if len(text) <= 650 else text[:647] + "..."
                self.bot.send_message(
                    chat, f"{title}\nVariant {key} (comparison preview):\n\n{preview}"
                )
            self.bot.send_message(
                chat,
                "Select a variant to review in full. Selection does not publish.",
                reply_markup=draft_keyboard(
                    draft,
                    [
                        [(f"Select {key}", key) for key in ("A", "B", "C")],
                        [("Regenerate text", "variants"), ("Cancel", "cancel")],
                    ],
                ),
            )
            return
        if draft.status in {"editing_text", "editing_idea"}:
            instruction = (
                "Send the full replacement LinkedIn text. The AI-assistance disclosure will be included in the next preview."
                if draft.status == "editing_text"
                else "Describe your image direction (up to 1,000 characters). For example: no robots; show a clean abstract network."
            )
            self.bot.send_message(
                chat,
                title + "\n" + instruction,
                reply_markup=draft_keyboard(
                    draft, [[("Back without changes", "back"), ("Cancel draft", "cancel")]]
                ),
            )
            return
        if draft.status != "review":
            raise ValueError("Unknown draft state")
        image_available = True
        if draft.data.get("image_path"):
            try:
                path = self.flow.image_path(draft)
            except ValueError:
                image_available = False
                self.bot.send_message(
                    chat,
                    "The reviewed image is missing or damaged. Regenerate it or remove it to continue text-only.",
                )
            else:
                with path.open("rb") as image:
                    self.bot.send_photo(
                        chat,
                        image,
                        caption=title
                        + "\nAI-generated illustration. Use Download original for the publication file.",
                    )
        if draft.data.get("post_text"):
            self.bot.send_message(
                chat, title + "\nLinkedIn — complete text:\n\n" + draft.data["post_text"]
            )
        if draft.data.get("x_thread"):
            for tweet in draft.data["x_thread"]:
                self.bot.send_message(chat, title + "\nX — exact reviewed tweet:\n\n" + tweet)
        brief = draft.data.get("brief")
        if brief:
            self.bot.send_message(chat, "Image concept:\n" + brief["concept"])
        rows = []
        has_image = bool(draft.data.get("image_path"))
        image_ok = not has_image or (
            image_available and self.config.images.enabled and self.flow.image_matches(draft)
        )
        if image_ok:
            destination = (
                "LinkedIn + X"
                if draft.data["feed_type"] == "both"
                else "X" if draft.data["feed_type"] == "twitter" else "LinkedIn"
            )
            rows.append(
                [
                    (
                        f"Publish {destination}" + (" + image" if has_image else " (text only)"),
                        "publish",
                    )
                ]
            )
        if draft.data.get("post_text"):
            if self.config.images.enabled:
                if (
                    brief
                    and draft.data.get("image_generations", 0) < self.config.images.max_generations
                ):
                    rows.append(
                        [("Regenerate image" if has_image else "Generate image", "generate")]
                    )
                elif not brief:
                    rows.append([("Create image idea", "brief")])
                rows.append([("Change image instructions", "edit_idea")])
            rows.append([("Edit text", "edit_text")])
        if has_image:
            if (
                image_available
                and not self.flow.image_matches(draft)
                and self.config.images.enabled
            ):
                rows.append([("Keep image with updated text/idea", "keep")])
            rows.append([("Remove image", "remove")])
            if image_available:
                rows[-1].append(("Download original", "original"))
        rows.append([("Cancel", "cancel")])
        count = draft.data.get("image_generations", 0)
        self.bot.send_message(
            chat,
            title
            + f"\nReview all content above. Publish submits this version only.\nImage attempts: {count}/{self.config.images.max_generations}.",
            reply_markup=draft_keyboard(draft, rows),
        )


def register_handlers(bot: TeleBot, config: AppConfig) -> DraftBotController:
    return DraftBotController(bot, config)
