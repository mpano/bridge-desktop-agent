"""What happened with each connected service: sign-ins, disconnects and API calls.

Entries hold the service, a short action name and the outcome — never message content,
recipients, search terms or tokens.
"""

import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

# First path segment (or Slack method) → readable action. Anything else is "API request".
ACTIONS = {
    "gmail": {
        "messages": "Searched or read email",
        "messages/send": "Sent an email",
        "threads": "Read an email thread",
        "drafts": "Saved a draft",
        "profile": "Checked profile",
    },
    "google_calendar": {
        "users/me/calendarList": "Listed calendars",
        "GET events": "Read events",
        "POST events": "Created an event",
        "DELETE events": "Deleted an event",
    },
    "slack": {
        "search.messages": "Searched Slack",
        "conversations.list": "Listed channels",
        "conversations.history": "Read a channel",
        "conversations.open": "Opened a direct message",
        "users.list": "Looked up people",
        "chat.postMessage": "Sent a message",
    },
    "spotify": {
        "search": "Searched Spotify",
        "me/playlists": "Listed playlists",
        "me/player/devices": "Listed devices",
        "me/player/play": "Started playback",
        "me/player/queue": "Queued a track",
    },
}


def describe(provider: str, method: str, path: str) -> str:
    names = ACTIONS.get(provider, {})
    if provider == "google_calendar" and "/events" in path:
        return names.get(f"{method} events", "Calendar request")
    for key in sorted(names, key=len, reverse=True):
        if path == key or path.startswith(key + "/"):
            return names[key]
    return "API request"


class MemoryActivity:
    def __init__(self, limit: int = 200):
        self._items: deque[dict] = deque(maxlen=limit)
        self._guard = threading.Lock()

    def record(self, provider: str, event: str, ok: bool = True, detail: str = "", account=""):
        entry = {
            "at": time.time(),
            "provider": provider,
            "account": account,
            "event": event,
            "ok": ok,
            "detail": detail[:300],
        }
        with self._guard:
            self._items.append(entry)
        self._persist(entry)

    def _persist(self, entry: dict) -> None:
        pass

    def recent(self, limit: int = 100) -> list[dict]:
        with self._guard:
            return list(self._items)[-limit:][::-1]


class SQLiteActivity(MemoryActivity):
    """Keeps the last 500 entries across restarts, in Bridge's private local database."""

    def __init__(self, path: Path | str, limit: int = 200):
        super().__init__(limit)
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS connection_activity (id INTEGER PRIMARY KEY "
                "AUTOINCREMENT, at REAL, provider TEXT, account TEXT, event TEXT, ok INTEGER, "
                "detail TEXT)"
            )
            rows = db.execute(
                "SELECT at,provider,account,event,ok,detail FROM connection_activity "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        keys = ("at", "provider", "account", "event", "ok", "detail")
        for row in reversed(rows):
            self._items.append({**dict(zip(keys, row, strict=True)), "ok": bool(row[4])})

    def _persist(self, entry: dict) -> None:
        try:
            with sqlite3.connect(self.path) as db:
                db.execute(
                    "INSERT INTO connection_activity (at,provider,account,event,ok,detail) "
                    "VALUES (?,?,?,?,?,?)",
                    tuple(
                        entry[key] for key in ("at", "provider", "account", "event", "ok", "detail")
                    ),
                )
                db.execute(
                    "DELETE FROM connection_activity WHERE id <= "
                    "(SELECT MAX(id) - 500 FROM connection_activity)"
                )
        except sqlite3.Error:
            pass  # The log is a convenience; never fail a request because of it.
