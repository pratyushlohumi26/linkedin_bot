"""SQLite-backed, owner-bound drafts with optimistic revision checks."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TERMINAL_STATUSES = {"published", "partial", "uncertain", "cancelled", "expired"}
BUSY_STATUSES = {
    "scraping",
    "reading_source",
    "generating_text",
    "generating_brief",
    "generating_image",
    "publishing",
}


class DraftConflict(ValueError):
    """The requested draft no longer matches the version the user reviewed."""


@dataclass(frozen=True)
class Draft:
    id: str
    chat_id: int
    user_id: int
    revision: int
    status: str
    data: dict[str, Any]
    updated_at: float


class DraftStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                revision INTEGER NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
                updated_at REAL NOT NULL
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS draft_owner ON drafts(chat_id, user_id, updated_at)"
            )

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _draft(row: sqlite3.Row) -> Draft:
        return Draft(
            id=row["id"],
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            revision=row["revision"],
            status=row["status"],
            data=json.loads(row["data"]),
            updated_at=row["updated_at"],
        )

    def create(self, chat_id: int, user_id: int, feed_type: str) -> Draft:
        if feed_type not in {"linkedin", "twitter", "both"}:
            raise ValueError("Unknown destination")
        now = time.time()
        draft_id = uuid.uuid4().hex[:16]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT status FROM drafts WHERE chat_id=? AND user_id=? AND status='publishing'",
                (chat_id, user_id),
            ).fetchone()
            if active:
                raise DraftConflict(
                    "Publication is in progress. Wait before starting another draft."
                )
            db.execute(
                """UPDATE drafts SET status='cancelled', revision=revision+1, updated_at=?
                WHERE chat_id=? AND user_id=? AND status NOT IN
                ('published','partial','uncertain','cancelled','expired')""",
                (now, chat_id, user_id),
            )
            db.execute(
                "INSERT INTO drafts VALUES (?, ?, ?, 1, 'awaiting_url', ?, ?)",
                (draft_id, chat_id, user_id, json.dumps({"feed_type": feed_type}), now),
            )
            snapshot = self._draft(
                db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            )
        return snapshot

    def get(self, draft_id: str, chat_id: int, user_id: int) -> Draft:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM drafts WHERE id=? AND chat_id=? AND user_id=?",
                (draft_id, chat_id, user_id),
            ).fetchone()
        if row is None:
            raise DraftConflict("Draft not found for this user/chat. Use /start_post.")
        return self._draft(row)

    def latest(self, chat_id: int, user_id: int) -> Draft | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM drafts WHERE chat_id=? AND user_id=? ORDER BY rowid DESC LIMIT 1",
                (chat_id, user_id),
            ).fetchone()
        return self._draft(row) if row else None

    def update(
        self, draft: Draft, *, status: str | None = None, changes: dict[str, Any] | None = None
    ) -> Draft:
        data = {**draft.data, **(changes or {})}
        with self.connection() as db:
            result = db.execute(
                """UPDATE drafts SET data=?, status=?, revision=revision+1, updated_at=?
                WHERE id=? AND chat_id=? AND user_id=? AND revision=? AND status=?""",
                (
                    json.dumps(data),
                    status or draft.status,
                    time.time(),
                    draft.id,
                    draft.chat_id,
                    draft.user_id,
                    draft.revision,
                    draft.status,
                ),
            )
            if result.rowcount != 1:
                raise DraftConflict("This preview is outdated. Use /resume for the latest version.")
            # Return our own revision, never a cancellation committed immediately afterward.
            snapshot = self._draft(
                db.execute("SELECT * FROM drafts WHERE id=?", (draft.id,)).fetchone()
            )
        return snapshot

    def recover_interrupted(self) -> int:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM drafts WHERE status IN ('scraping','reading_source','generating_text','generating_brief','generating_image','publishing')"
            ).fetchall()
        recovered = 0
        for row in rows:
            draft = self._draft(row)
            changes: dict[str, Any] = {
                "notice": "The previous operation was interrupted. Nothing will be retried automatically."
            }
            if draft.status == "publishing":
                for platform in ("linkedin", "twitter"):
                    if draft.data.get(platform + "_status") == "publishing":
                        changes[platform + "_status"] = "uncertain"
                platforms = (
                    ("linkedin", "twitter")
                    if draft.data["feed_type"] == "both"
                    else (draft.data["feed_type"],)
                )
                states = {
                    changes.get(p + "_status", draft.data.get(p + "_status")) for p in platforms
                }
                if states == {"published"}:
                    status = "published"
                    changes["notice"] = (
                        "Publication completed before the restart. No retry is needed."
                    )
                elif "uncertain" in states:
                    status = "uncertain"
                elif "published" in states:
                    status = "partial"
                else:
                    status = "review"
            elif draft.status == "reading_source":
                status = "awaiting_source_text"
                changes["notice"] = (
                    "Text-file reading was interrupted. Previously saved parts remain; upload the file again."
                )
            elif draft.status == "scraping":
                status = "source_recovery"
                changes["source_metadata"] = {"status": "interrupted", "retryable": True}
            elif draft.data.get("post_text") or draft.data.get("x_thread"):
                status = "review"
            elif draft.data.get("variants"):
                status = "variants"
            elif draft.data.get("article_text"):
                status = "source_review"
            else:
                status = "awaiting_url"
            try:
                self.update(draft, status=status, changes=changes)
                recovered += 1
            except DraftConflict:
                pass
        return recovered

    def expire(self, retention_days: int) -> list[str]:
        cutoff = time.time() - retention_days * 86400
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id FROM drafts WHERE updated_at < ? AND status NOT IN ('scraping','reading_source','generating_text','generating_brief','generating_image','publishing')",
                (cutoff,),
            ).fetchall()
            ids = [row["id"] for row in rows]
            for draft_id in ids:
                db.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
        return ids
