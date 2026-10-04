"""Recent chats in Ask, kept on this Mac for a few days (Settings › Privacy › Keep chats).

Only what you saw is stored: your words, Bridge's replies and which steps ran, plus the short
context the model gets back when you continue a chat. Never approval tokens or account
details. Chats older than the chosen number of days are deleted; 0 days keeps nothing.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

RETENTION_CHOICES = (0, 7, 30)
# A new message after this long starts a new chat; the old one stays in Recent.
IDLE_SECONDS = 4 * 3600
LIST_LIMIT = 30


def title_of(transcript: list[dict]) -> str:
    first = next((e for e in transcript if e.get("role") == "user"), None)
    return " ".join(str(first["text"]).split())[:70] if first else "New conversation"


class ChatStore:
    def __init__(self, path: Path | str, days: int = 7, clock=time.time):
        self.path, self.days, self.clock = str(path), days, clock
        with self._db() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, started REAL NOT NULL,
                    updated REAL NOT NULL, transcript TEXT NOT NULL, history TEXT NOT NULL)"""
            )
        self.prune()

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    @property
    def keeping(self) -> bool:
        return self.days > 0

    @staticmethod
    def new_id() -> str:
        return uuid4().hex

    def save(self, chat_id: str, transcript: list[dict], history: list[dict]) -> None:
        if not self.keeping or not transcript:
            return
        now = self.clock()
        with self._db() as db:
            db.execute(
                """INSERT INTO chats (id, title, started, updated, transcript, history)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET title = excluded.title, updated = excluded.updated,
                    transcript = excluded.transcript, history = excluded.history""",
                (
                    chat_id,
                    title_of(transcript),
                    now,
                    now,
                    json.dumps(transcript, ensure_ascii=False),
                    json.dumps(history, ensure_ascii=False),
                ),
            )

    def load(self, chat_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute(
                "SELECT id, updated, transcript, history FROM chats WHERE id = ?", (chat_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "updated": row[1],
            "transcript": json.loads(row[2]),
            "history": json.loads(row[3]),
        }

    def latest(self) -> dict | None:
        """The chat to carry on after a restart: the newest, if it isn't stale."""
        with self._db() as db:
            row = db.execute(
                "SELECT id, updated FROM chats ORDER BY updated DESC LIMIT 1"
            ).fetchone()
        if row is None or self.clock() - row[1] > IDLE_SECONDS:
            return None
        return self.load(row[0])

    def recent(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT id, title, started, updated, json_array_length(transcript) FROM chats "
                "ORDER BY updated DESC LIMIT ?",
                (LIST_LIMIT,),
            ).fetchall()
        return [
            {"id": r[0], "title": r[1], "started": r[2], "updated": r[3], "messages": r[4]}
            for r in rows
        ]

    def delete(self, chat_id: str) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM chats WHERE id = ?", (chat_id,)).rowcount > 0

    def delete_all(self) -> int:
        with self._db() as db:
            return db.execute("DELETE FROM chats").rowcount

    def set_days(self, days: int) -> None:
        self.days = days
        self.prune()

    def prune(self) -> int:
        if not self.keeping:
            return self.delete_all()
        cutoff = self.clock() - self.days * 86400
        with self._db() as db:
            return db.execute("DELETE FROM chats WHERE updated < ?", (cutoff,)).rowcount
