"""Bounded cache for explicitly cacheable public HTTP articles only."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class ScrapeCache:
    def __init__(self, path: str, max_entries: int):
        self.path = Path(path)
        self.max_entries = max_entries
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS articles (key TEXT PRIMARY KEY, expires REAL NOT NULL, payload TEXT NOT NULL)"
            )

    def get(self, key: str) -> dict | None:
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.execute("DELETE FROM articles WHERE expires <= ?", (time.time(),))
            row = db.execute("SELECT payload FROM articles WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, payload: dict, ttl: int):
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO articles VALUES (?, ?, ?)",
                (key, time.time() + ttl, json.dumps(payload)),
            )
            db.execute(
                "DELETE FROM articles WHERE key NOT IN (SELECT key FROM articles ORDER BY expires DESC LIMIT ?)",
                (self.max_entries,),
            )
