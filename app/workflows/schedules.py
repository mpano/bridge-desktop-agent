"""Saved recurring requests ("every weekday at 9, brief me"), stored in the local SQLite DB."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

Kind = Literal["once", "daily", "weekdays", "weekly"]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@dataclass(frozen=True)
class Schedule:
    id: int
    message: str
    kind: Kind
    at: str  # "HH:MM" local wall time; for "once" the full ISO date-time
    weekday: int | None
    next_run: str | None  # aware ISO date-time, None once finished
    last_status: str | None

    def describe(self) -> str:
        if self.kind == "once":
            return "once at " + datetime.fromisoformat(self.at).strftime("%a %d %b %H:%M")
        if self.kind == "daily":
            return f"every day at {self.at}"
        if self.kind == "weekdays":
            return f"every weekday at {self.at}"
        return f"every {WEEKDAYS[self.weekday].capitalize()} at {self.at}"


def local(day: date, clock: time) -> datetime:
    # Attaching the local zone per date keeps wall-clock times correct across DST changes.
    return datetime.combine(day, clock).astimezone()


def next_occurrence(kind: Kind, at: str, weekday: int | None, after: datetime) -> datetime | None:
    if kind == "once":
        moment = datetime.fromisoformat(at)
        return moment if moment > after else None
    clock = time.fromisoformat(at)
    day = after.date()
    for _ in range(8):
        candidate = local(day, clock)
        allowed = (
            kind == "daily"
            or (kind == "weekdays" and day.weekday() < 5)
            or (kind == "weekly" and day.weekday() == weekday)
        )
        if allowed and candidate > after:
            return candidate
        day += timedelta(days=1)
    return None


class ScheduleStore:
    MAX_SCHEDULES = 50

    def __init__(self, path: Path | str):
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS schedules (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "message TEXT NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL, weekday INTEGER, "
                "next_run TEXT, last_status TEXT, "
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )

    @staticmethod
    def _row(row) -> Schedule:
        return Schedule(*row)

    def list(self) -> list[Schedule]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT id,message,kind,at,weekday,next_run,last_status FROM schedules ORDER BY id"
            ).fetchall()
        return [self._row(row) for row in rows]

    def add(
        self, message: str, kind: Kind, at: str, weekday: int | None, now: datetime
    ) -> Schedule:
        first = next_occurrence(kind, at, weekday, now)
        if first is None:
            raise ValueError("That time has already passed. Choose a future time.")
        with sqlite3.connect(self.path) as db:
            (count,) = db.execute("SELECT COUNT(*) FROM schedules").fetchone()
            if count >= self.MAX_SCHEDULES:
                raise ValueError("Too many scheduled tasks. Delete some first.")
            cursor = db.execute(
                "INSERT INTO schedules (message,kind,at,weekday,next_run) VALUES (?,?,?,?,?)",
                (message, kind, at, weekday, first.isoformat()),
            )
            new_id = cursor.lastrowid
        return next(item for item in self.list() if item.id == new_id)

    def delete(self, schedule_id: int) -> bool:
        with sqlite3.connect(self.path) as db:
            return db.execute("DELETE FROM schedules WHERE id=?", (schedule_id,)).rowcount > 0

    def due(self, now: datetime) -> list[Schedule]:
        return [
            item
            for item in self.list()
            if item.next_run and datetime.fromisoformat(item.next_run) <= now
        ]

    def advance(self, item: Schedule, now: datetime, status: str) -> None:
        """Move to the next occurrence before running, so a crash can't repeat a run."""
        upcoming = next_occurrence(item.kind, item.at, item.weekday, now)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE schedules SET next_run=?, last_status=? WHERE id=?",
                (upcoming.isoformat() if upcoming else None, status, item.id),
            )

    def record(self, schedule_id: int, status: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE schedules SET last_status=? WHERE id=?", (status, schedule_id))
