"""Proactive assistant state: watches ("tell me when …"), settings, sent heads-ups."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MAX_WATCHES = 30
SEEN_LIMIT = 300
DEFAULTS = {
    "meeting_prep": True,
    "lead_minutes": 10,
    "evening_summary": False,
    "evening_time": "18:00",
    "evening_schedule_id": 0,
    "work_start": "09:00",
    "work_end": "18:00",
    "morning_plan": False,
    "morning_time": "08:30",
    "morning_schedule_id": 0,
    "onboarded": False,
}


ACTIVE = {"waiting", "overdue"}


@dataclass(frozen=True)
class Followup:
    id: int
    name: str
    email: str
    about: str
    since: float
    due: str  # ISO date-time with offset
    status: str  # waiting, overdue, replied, cancelled, expired
    last_checked: float | None


@dataclass(frozen=True)
class Watch:
    id: int
    kind: str  # "email" or "slack"
    query: str
    label: str
    seen: list[str]
    baseline_done: bool
    last_checked: float | None
    last_error: str | None
    created_at: float


class ProactiveStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, query TEXT NOT NULL,
                    label TEXT NOT NULL, seen TEXT NOT NULL DEFAULT '[]',
                    baseline_done INTEGER NOT NULL DEFAULT 0, last_checked REAL,
                    last_error TEXT, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS proactive_settings (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS proactive_notified (key TEXT PRIMARY KEY, at REAL);
                CREATE TABLE IF NOT EXISTS followups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    email TEXT NOT NULL, about TEXT NOT NULL, since REAL NOT NULL,
                    due TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
                    last_checked REAL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS day_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
                    blocks TEXT NOT NULL, created_at REAL NOT NULL);
                """
            )

    def _db(self):
        return sqlite3.connect(self.path)

    # Watches ------------------------------------------------------------------------------

    def watches(self) -> list[Watch]:
        with self._db() as db:
            rows = db.execute(
                "SELECT id, kind, query, label, seen, baseline_done, last_checked, last_error, "
                "created_at FROM watches ORDER BY id"
            ).fetchall()
        return [
            Watch(row[0], row[1], row[2], row[3], json.loads(row[4]), bool(row[5]), *row[6:])
            for row in rows
        ]

    def add_watch(self, kind: str, query: str, label: str) -> Watch:
        existing = self.watches()
        for watch in existing:
            if watch.kind == kind and watch.query.casefold() == query.casefold():
                return watch
        if len(existing) >= MAX_WATCHES:
            raise ValueError("You have too many watches. Remove some first.")
        with self._db() as db:
            cursor = db.execute(
                "INSERT INTO watches (kind, query, label, created_at) VALUES (?, ?, ?, ?)",
                (kind, query, label, time.time()),
            )
            new_id = cursor.lastrowid
        return next(watch for watch in self.watches() if watch.id == new_id)

    def remove_watch(self, watch_id: int) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM watches WHERE id = ?", (watch_id,)).rowcount > 0

    def checked(self, watch_id: int, seen: list[str], error: str | None = None) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE watches SET seen = ?, baseline_done = 1, last_checked = ?, last_error = ? "
                "WHERE id = ?",
                (json.dumps(seen[-SEEN_LIMIT:]), time.time(), error, watch_id),
            )

    def failed(self, watch_id: int, error: str) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE watches SET last_checked = ?, last_error = ? WHERE id = ?",
                (time.time(), error[:300], watch_id),
            )

    # Settings -----------------------------------------------------------------------------

    def settings(self) -> dict:
        with self._db() as db:
            rows = dict(db.execute("SELECT key, value FROM proactive_settings").fetchall())
        return {
            key: json.loads(rows[key]) if key in rows else value for key, value in DEFAULTS.items()
        }

    def update_settings(self, **values) -> dict:
        unknown = set(values) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown proactive settings: {', '.join(sorted(unknown))}")
        with self._db() as db:
            db.executemany(
                "INSERT INTO proactive_settings VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(key, json.dumps(value)) for key, value in values.items() if value is not None],
            )
        return self.settings()

    # Heads-ups already sent (so a restart never repeats one) ------------------------------

    def notify_once(self, key: str) -> bool:
        with self._db() as db:
            db.execute("DELETE FROM proactive_notified WHERE at < ?", (time.time() - 3 * 86400,))
            try:
                db.execute("INSERT INTO proactive_notified VALUES (?, ?)", (key, time.time()))
            except sqlite3.IntegrityError:
                return False
        return True

    # Follow-ups ("remind me if Olivier doesn't reply by Friday") ---------------------------

    def add_followup(self, name: str, email: str, about: str, due: str) -> Followup:
        active = [item for item in self.followups() if item.status in ACTIVE]
        if len(active) >= MAX_WATCHES:
            raise ValueError("You're tracking too many follow-ups. Cancel some first.")
        now = time.time()
        with self._db() as db:
            cursor = db.execute(
                "INSERT INTO followups (name, email, about, since, due, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, email, about, now, due, now),
            )
            new_id = cursor.lastrowid
        return next(item for item in self.followups() if item.id == new_id)

    def followups(self) -> list[Followup]:
        with self._db() as db:
            rows = db.execute(
                "SELECT id, name, email, about, since, due, status, last_checked "
                "FROM followups ORDER BY id"
            ).fetchall()
        return [Followup(*row) for row in rows]

    def set_followup(self, followup_id: int, status: str) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE followups SET status = ?, last_checked = ? WHERE id = ?",
                (status, time.time(), followup_id),
            )

    # Day plans ----------------------------------------------------------------------------

    def save_plan(self, day: str, blocks: list[dict]) -> int:
        with self._db() as db:
            db.execute("DELETE FROM day_plans WHERE created_at < ?", (time.time() - 7 * 86400,))
            cursor = db.execute(
                "INSERT INTO day_plans (day, blocks, created_at) VALUES (?, ?, ?)",
                (day, json.dumps(blocks), time.time()),
            )
            return cursor.lastrowid

    def latest_plan(self, day: str) -> tuple[int, list[dict]] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT id, blocks FROM day_plans WHERE day = ? ORDER BY id DESC LIMIT 1", (day,)
            ).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def plan(self, plan_id: int) -> tuple[str, list[dict]] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT day, blocks FROM day_plans WHERE id = ?", (plan_id,)
            ).fetchone()
        return (row[0], json.loads(row[1])) if row else None
