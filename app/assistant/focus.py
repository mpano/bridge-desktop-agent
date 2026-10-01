"""Focus mode: block the calendar, quiet Slack, start focus music, then tell you what you
missed.

Every part is optional and independent: one that isn't connected (or fails) is skipped
with a reason, never stopping the others. Slack's status and Do Not Disturb expire on
Slack's side at the end time, so they clear even if Bridge is closed. Nothing is sent to
anyone; the only visible change for others is your own Slack status and a busy block.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.agent.proactive import slack_query

DEEP_FOCUS = "spotify:playlist:37i9dQZF1DWZeKCadgRdKQ"  # Spotify's own "Deep Focus".
PROFILE_READ, PROFILE_WRITE, DND_WRITE = "users.profile:read", "users.profile:write", "dnd:write"
SLACK_RECONNECT = "Reconnect Slack in Connections to let Bridge set your status and Do Not Disturb."


class FocusStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        with self._db() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS focus_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL,
                    start REAL NOT NULL, end REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active', undo TEXT NOT NULL DEFAULT '{}',
                    ended_at REAL)"""
            )

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    def active(self) -> dict | None:
        with self._db() as db:
            row = db.execute(
                "SELECT id, task, start, end, undo FROM focus_sessions WHERE status = 'active' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "task": row[1],
            "start": row[2],
            "end": row[3],
            "undo": json.loads(row[4]),
        }

    def add(self, task: str, start: float, end: float, undo: dict) -> int:
        with self._db() as db:
            return db.execute(
                "INSERT INTO focus_sessions (task, start, end, undo) VALUES (?, ?, ?, ?)",
                (task, start, end, json.dumps(undo)),
            ).lastrowid

    def close(self, session_id: int, status: str) -> bool:
        """True only for the caller that actually ended it (end timer vs. "stop")."""
        with self._db() as db:
            return (
                db.execute(
                    "UPDATE focus_sessions SET status = ?, ended_at = ? "
                    "WHERE id = ? AND status = 'active'",
                    (status, time.time(), session_id),
                ).rowcount
                > 0
            )


def _clock(at: float) -> str:
    return datetime.fromtimestamp(at).astimezone().strftime("%H:%M")


class FocusMode:
    def __init__(
        self,
        store: FocusStore,
        notify: Callable[[str, str], Awaitable[None]] | None = None,
        *,
        calendar=None,
        accounts=None,
        spotify_web=None,
        spotify_desktop=None,
        gmail=None,
        slack=None,
        default_playlist: str = "Deep Focus",
        clock: Callable[[], float] = time.time,
    ):
        self.store, self.notify = store, notify
        self.calendar, self.accounts = calendar, accounts
        self.spotify_web, self.spotify_desktop = spotify_web, spotify_desktop
        self.gmail, self.slack = gmail, slack
        self.default_playlist, self.clock = default_playlist, clock
        self._timer: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # Start -------------------------------------------------------------------------------

    async def start(
        self,
        minutes: int,
        task: str = "",
        *,
        calendar=True,
        slack=True,
        music=True,
        playlist: str | None = None,
    ) -> dict:
        async with self._lock:
            current = self.store.active()
            if current and current["end"] > self.clock():
                raise ValueError(
                    f"You're already focusing until {_clock(current['end'])}. "
                    "Say “stop focusing” first."
                )
            if current:
                await self._finish(current, "done")
            start = self.clock()
            end = start + minutes * 60
            task = " ".join(task.split())[:80]
            done, skipped, undo = [], [], {}
            parts = [
                ("calendar", calendar, self._block_calendar),
                ("slack", slack, self._quiet_slack),
                ("music", music, self._play_music),
            ]
            for name, wanted, step in parts:
                if not wanted:
                    continue
                try:
                    label, saved = await step(task, start, end, playlist)
                    done.append(label)
                    undo[name] = saved
                except Exception as exc:
                    skipped.append(f"{name.capitalize()}: {self._reason(name, exc)}")
            session_id = self.store.add(task, start, end, undo)
            self._schedule_end(session_id, end)
            return {
                "id": session_id,
                "task": task,
                "minutes": minutes,
                "until": _clock(end),
                "done": done,
                "skipped": skipped,
            }

    @staticmethod
    def _reason(name: str, exc: Exception) -> str:
        text = str(exc) or "it didn't work"
        if name == "slack" and ("permission" in text.casefold() or "scope" in text.casefold()):
            return SLACK_RECONNECT
        return text[:160]

    async def _block_calendar(self, task, start, end, _playlist):
        if self.calendar is None:
            raise ValueError("Calendar isn't available.")
        title = f"🎯 Focus: {task}" if task else "🎯 Focus time"
        begin, finish = (
            datetime.fromtimestamp(start).astimezone(),
            datetime.fromtimestamp(end).astimezone(),
        )
        await asyncio.to_thread(
            self.calendar.backend.create, title, begin, finish, None, "", "Focus mode by Bridge."
        )
        return f"Blocked your calendar until {_clock(end)}", {
            "title": title,
            "start": begin.isoformat(),
            "end": finish.isoformat(),
        }

    async def _slack(self, verb: str, method: str, scope: str, **kwargs) -> dict:
        return await self.accounts.request(None, "slack", verb, method, scopes=(scope,), **kwargs)

    async def _quiet_slack(self, task, start, end, _playlist):
        if self.accounts is None:
            raise ValueError("Connect Slack in Connections first.")
        profile = (await self._slack("GET", "users.profile.get", PROFILE_READ)).get("profile", {})
        previous = {
            "status_text": profile.get("status_text", ""),
            "status_emoji": profile.get("status_emoji", ""),
            "status_expiration": int(profile.get("status_expiration") or 0),
        }
        text = f"Focusing on {task}" if task else "Focusing"
        await self._slack(
            "POST",
            "users.profile.set",
            PROFILE_WRITE,
            json={
                "profile": {
                    "status_text": f"{text} until {_clock(end)}"[:100],
                    "status_emoji": ":dart:",
                    "status_expiration": int(end),  # Slack clears it itself at the end.
                }
            },
        )
        await self._slack(
            "POST",
            "dnd.setSnooze",
            DND_WRITE,
            params={"num_minutes": max(1, round((end - start) / 60))},
        )
        return "Set your Slack status and paused notifications", previous

    async def _play_music(self, _task, _start, _end, playlist):
        if self.spotify_desktop is None:
            raise ValueError("Spotify isn't available.")
        name, uri = await self._find_playlist(playlist)
        await self.spotify_desktop.play_uri(uri)
        return f"Playing “{name}” on Spotify", {"uri": uri, "name": name}

    async def _find_playlist(self, wanted: str | None) -> tuple[str, str]:
        """Your own playlists first, then Spotify search. Spotify's API no longer returns
        its own editorial playlists to new apps, so the default "Deep Focus" falls back
        to its well-known address, which the Spotify app plays directly."""
        query = (wanted or "").strip() or self.default_playlist
        if self.spotify_web is not None:
            words = query.casefold().split()
            with contextlib.suppress(Exception):
                mine = await self.spotify_web.playlists(SimpleNamespace(account_id=None))
                for item in mine["playlists"]:
                    if item.get("uri") and all(w in (item["name"] or "").casefold() for w in words):
                        return item["name"], item["uri"]
            with contextlib.suppress(Exception):
                found = await self.spotify_web.search(
                    SimpleNamespace(account_id=None, query=query, kind="playlist", limit=10)
                )
                for item in found["items"]:  # Spotify sometimes returns empty entries.
                    if item.get("uri"):
                        return item["name"] or query, item["uri"]
        if not wanted:
            return "Deep Focus", DEEP_FOCUS
        if self.spotify_web is None:
            raise ValueError("Connect Spotify in Connections to pick a playlist by name.")
        raise ValueError(f"No Spotify playlist matches “{query}”.")

    # End ---------------------------------------------------------------------------------

    def _schedule_end(self, session_id: int, end: float) -> None:
        if self._timer is not None:
            self._timer.cancel()

        async def wait():
            await asyncio.sleep(max(0.0, end - self.clock()))
            await self.finish(session_id, "done")

        self._timer = asyncio.create_task(wait())

    async def stop(self) -> dict:
        current = self.store.active()
        if current is None:
            raise ValueError("You're not in focus mode.")
        return await self.finish(current["id"], "stopped")

    async def check(self) -> None:
        """Called every minute: ends a session whose time passed (e.g. after a restart)."""
        current = self.store.active()
        if current and current["end"] <= self.clock():
            await self.finish(current["id"], "done")

    async def finish(self, session_id: int, status: str) -> dict:
        async with self._lock:
            current = self.store.active()
            if current is None or current["id"] != session_id:
                return {"status": "already_ended"}
            return await self._finish(current, status)

    async def _finish(self, session: dict, status: str) -> dict:
        if not self.store.close(session["id"], status):
            return {"status": "already_ended"}
        if self._timer is not None and status == "stopped":
            self._timer.cancel()
            self._timer = None
        now = min(self.clock(), session["end"])
        undo = session["undo"]
        early = status == "stopped" and now < session["end"] - 60
        for step in (self._restore_slack, self._pause_music):
            try:
                await step(undo, early)
            except Exception:
                pass  # Slack expires the status itself; music can be paused by hand.
        if early and "calendar" in undo:
            try:
                await self._shorten_event(undo["calendar"], now)
            except Exception:
                pass
        missed = await self._missed(session["start"])
        minutes = max(1, round((now - session["start"]) / 60))
        about = f" on {session['task']}" if session["task"] else ""
        title = "Focus done ✓" if status == "done" else "Focus stopped"
        summary = f"{minutes} min{about}. " + (missed["summary"] or "Nothing new came in.")
        if self.notify is not None:
            try:
                await self.notify(title, summary)
            except Exception:
                pass
        return {"status": status, "minutes": minutes, "task": session["task"], **missed}

    async def _restore_slack(self, undo: dict, early: bool) -> None:
        previous = undo.get("slack")
        if previous is None:
            return
        if previous["status_expiration"] and previous["status_expiration"] <= self.clock():
            previous = {"status_text": "", "status_emoji": "", "status_expiration": 0}
        await self._slack("POST", "users.profile.set", PROFILE_WRITE, json={"profile": previous})
        if early:
            await self._slack("POST", "dnd.endSnooze", DND_WRITE)

    async def _pause_music(self, undo: dict, _early: bool) -> None:
        if "music" in undo and self.spotify_desktop is not None:
            playing = await self.spotify_desktop.now_playing()
            if playing.get("state") == "playing":
                await self.spotify_desktop.control(SimpleNamespace(action="pause"))

    async def _shorten_event(self, event: dict, now: float) -> None:
        start = datetime.fromisoformat(event["start"])
        end = datetime.fromisoformat(event["end"])
        window = (start - timedelta(minutes=1), end + timedelta(minutes=1))
        await asyncio.to_thread(self.calendar.backend.delete, event["title"], *window)
        if now - start.timestamp() >= 5 * 60:  # Keep the time actually spent focusing.
            await asyncio.to_thread(
                self.calendar.backend.create,
                event["title"],
                start,
                datetime.fromtimestamp(now).astimezone(),
                None,
                "",
                "Focus mode by Bridge.",
            )

    async def _missed(self, since: float) -> dict:
        mentions, emails, parts = [], [], []
        if self.slack is not None and self.accounts is not None:
            try:
                listed = (await self.accounts.list_accounts())["accounts"]
                identity = next((a["identity"] for a in listed if a["provider"] == "slack"), None)
                query = slack_query("mentions", identity)
                if query != "mentions":
                    found = await self.slack.search(
                        SimpleNamespace(account_id=None, query=query, limit=20)
                    )
                    mentions = [
                        item for item in found["messages"] if float(item.get("ts") or 0) >= since
                    ]
            except Exception:
                pass
        if self.gmail is not None:
            try:
                found = await self.gmail.search(
                    SimpleNamespace(
                        account_id=None, query=f"in:inbox is:unread after:{int(since)}", limit=20
                    )
                )
                emails = found["messages"]
            except Exception:
                pass
        if mentions:
            people = sorted({str(item.get("user") or "someone") for item in mentions})[:3]
            plural = "s" if len(mentions) != 1 else ""
            parts.append(f"{len(mentions)} Slack mention{plural} ({', '.join(people)})")
        if emails:
            senders = sorted({m["from"].split("<")[0].strip().strip('"') for m in emails})[:3]
            plural = "s" if len(emails) != 1 else ""
            parts.append(f"{len(emails)} new email{plural} ({', '.join(senders)})")
        summary = "While you focused: " + " and ".join(parts) + "." if parts else ""
        return {"slack_mentions": len(mentions), "emails": len(emails), "summary": summary}

    # Status ------------------------------------------------------------------------------

    def status(self) -> dict:
        current = self.store.active()
        if current is None:
            return {"active": False}
        left = max(0, round((current["end"] - self.clock()) / 60))
        return {
            "active": True,
            "task": current["task"],
            "until": _clock(current["end"]),
            "minutes_left": left,
        }
