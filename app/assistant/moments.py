"""Reminders for a moment, not a time.

"Remind me to ask about the budget when the standup starts", "when I open Figma, show me
Ana's feedback", "remind me to call the bank when I'm back at my desk". Bridge notices the
moment (the app in front, a calendar meeting starting, you coming back after five minutes
away) and reminds you then. It only reads which app is in front and how long the Mac has
been idle; nothing leaves the Mac. A small check runs every few seconds, and only while you
have such reminders.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.llm.structured import parse_json
from app.llm.usage import task

AWAY_SECONDS = 300  # Away this long, then any input means "I'm back".
TICK = 3.0
MAX_ACTIVE = 40

APP = re.compile(
    r"\bwhen(?:ever)? (?:i )?(?:open|launch|switch to|go (?:in)?to|start using|use) "
    r"(?:the )?(?P<app>[\w .+&'-]+?)(?: app)?(?=[,.!?]|\s+(?:remind|to|and|show|tell)\b|$)",
    re.I,
)
BACK = re.compile(
    r"\bwhen(?:ever)? (?:i'?m|i am|i get|i come) back"
    r"(?: (?:at|to) my (?:desk|mac|computer|laptop))?",
    re.I,
)
MEETING = re.compile(
    r"\bwhen(?:ever)? (?:i (?:join|start|get into|hop on) (?:the |my )?(?P<a>.+?)"
    r"|(?:the |my )?(?P<b>.+?) (?:starts|begins|is starting))"
    r"(?: meeting| call)?(?=[,.!?]|\s+(?:remind|to|and|show|tell)\b|$)",
    re.I,
)
UNDERSTAND = """Turn the user's request into a reminder for a moment. Reply with JSON only:
{"kind": "app" | "meeting" | "back", "target": "the app name, or words from the meeting's
title ('' for any meeting)", "text": "what to remind them of, short, as an instruction"}
or {"error": "short reason"} if it's not a reminder for a moment."""


def understand_rules(words: str) -> dict | None:
    """The trigger and what to remind of, from plain words (None if unclear)."""
    text = " ".join(words.split())
    kind, target, span = None, "", None
    if m := BACK.search(text):
        kind, span = "back", m.span()
    elif m := APP.search(text):
        kind, target, span = "app", m.group("app").strip(), m.span()
    elif m := MEETING.search(text):
        kind, span = "meeting", m.span()
        target = (m.group("a") or m.group("b") or "").strip()
        target = re.sub(r"^(?:a|any|the|my)\s+", "", target, flags=re.I)
        target = re.sub(r"\b(meeting|call)s?$", "", target, flags=re.I).strip()
        if target.lower() in {"", "meeting", "call", "a meeting", "any meeting"}:
            target = ""
    if kind is None:
        return None
    rest = (text[: span[0]] + " " + text[span[1] :]).strip(" ,.")
    rest = re.sub(r"^(?:please\s+)?remind me(?: to)?\s*", "", rest, flags=re.I)
    rest = re.sub(r"\s*,?\s*remind me(?: to)?\s*", " ", rest, flags=re.I).strip(" ,.")
    if not rest:
        return None
    return {"kind": kind, "target": target, "text": rest[0].upper() + rest[1:]}


class MomentStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._pruned = 0.0
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS moments (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "text TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL, "
                "once INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'active', "
                "created REAL NOT NULL, fired_at REAL, fired INTEGER NOT NULL DEFAULT 0)"
            )

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    def all(self, active_only: bool = False) -> list[dict]:
        with self._db() as db:
            if time.time() - self._pruned > 3600:  # Old finished ones, hourly (reads are often).
                self._pruned = time.time()
                db.execute(
                    "DELETE FROM moments WHERE status = 'done' AND fired_at < ?",
                    (time.time() - 7 * 86400,),
                )
            rows = db.execute(
                "SELECT id, text, kind, target, once, status, created, fired_at, fired "
                "FROM moments "
                + ("WHERE status = 'active' " if active_only else "")
                + "ORDER BY id DESC"
            ).fetchall()
        keys = ("id", "text", "kind", "target", "once", "status", "created", "fired_at", "fired")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def add(self, text: str, kind: str, target: str, once: bool) -> int:
        with self._db() as db:
            return db.execute(
                "INSERT INTO moments (text, kind, target, once, created) VALUES (?, ?, ?, ?, ?)",
                (text, kind, target, int(once), time.time()),
            ).lastrowid

    def fired(self, moment_id: int, done: bool) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE moments SET fired = fired + 1, fired_at = ?, status = ? WHERE id = ?",
                (time.time(), "done" if done else "active", moment_id),
            )

    def remove(self, moment_id: int) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM moments WHERE id = ?", (moment_id,)).rowcount > 0


class MacSense:
    """What's happening on this Mac right now: the app in front, and how long it's been idle."""

    def frontmost(self) -> dict:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return {"app": "", "bundle_id": ""}
        return {
            "app": str(app.localizedName() or ""),
            "bundle_id": str(app.bundleIdentifier() or ""),
        }

    def idle_seconds(self) -> float:
        import Quartz

        return float(
            Quartz.CGEventSourceSecondsSinceLastEventType(
                Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType
            )
        )


class Moments:
    def __init__(self, path, *, calendar=None, llm=None, sense=None, clock=None):
        self.store = MomentStore(path)
        self.calendar, self.llm, self.sense = calendar, llm, sense
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.notify = None
        self._front = ""
        self._away = False
        self._meetings_at = 0.0
        self._met: set[str] = set()  # Meetings already announced.

    # Making one ---------------------------------------------------------------------------

    async def understand(self, words: str) -> dict:
        found = understand_rules(words)
        if found is None and self.llm is not None:
            try:
                with task("ask"):
                    answer = await self.llm.complete(UNDERSTAND, words)
                found = parse_json(answer) if isinstance(answer, str) else None
            except Exception:
                found = None  # Unclear either way: ask for a clearer "when".
            if isinstance(found, dict) and found.get("error"):
                raise ValueError(str(found["error"])[:200])
            if not (isinstance(found, dict) and found.get("kind") in {"app", "meeting", "back"}):
                found = None
        if found is None:
            raise ValueError(
                "Say when: “when I open Figma”, “when the standup starts”, or “when I'm back”."
            )
        return found

    async def add(self, words: str, *, once: bool | None = None) -> dict:
        found = await self.understand(words)
        if found["kind"] == "app" and not found["target"]:
            raise ValueError("Which app?")
        if len(self.store.all(active_only=True)) >= MAX_ACTIVE:
            raise ValueError("You have a lot of these already. Remove some first.")
        every = bool(re.search(r"\b(every time|whenever|each time)\b", words, re.I))
        moment_id = self.store.add(
            " ".join(str(found["text"]).split())[:300],
            found["kind"],
            " ".join(str(found.get("target") or "").split())[:80],
            not every if once is None else once,
        )
        return next(m for m in self.overview() if m["id"] == moment_id)

    @staticmethod
    def when(kind: str, target: str) -> str:
        if kind == "app":
            return f"When you open {target}"
        if kind == "meeting":
            return f"When “{target}” starts" if target else "When a meeting starts"
        return "When you're back at your Mac"

    def overview(self) -> list[dict]:
        return [{**m, "when": self.when(m["kind"], m["target"])} for m in self.store.all()]

    # Noticing the moment --------------------------------------------------------------------

    async def _fire(self, moment: dict, why: str) -> None:
        self.store.fired(moment["id"], done=bool(moment["once"]))
        if self.notify is not None:
            await self.notify(f"⏰ {moment['text']}", why, "today")

    async def tick(self) -> int:
        """Look once: which reminders' moment is now?"""
        active = self.store.all(active_only=True)
        if not active or self.sense is None:
            return 0
        fired = 0
        apps = [m for m in active if m["kind"] == "app"]
        if apps:
            front = await asyncio.to_thread(self.sense.frontmost)
            name = front.get("app", "")
            if name and name != self._front:
                for m in apps:
                    wanted = m["target"].lower()
                    if wanted and (
                        wanted in name.lower() or wanted in front.get("bundle_id", "").lower()
                    ):
                        await self._fire(m, f"You opened {name}.")
                        fired += 1
            self._front = name
        backs = [m for m in active if m["kind"] == "back"]
        idle = await asyncio.to_thread(self.sense.idle_seconds)
        if idle >= AWAY_SECONDS:
            self._away = True
        elif self._away and idle < TICK * 2:
            self._away = False
            for m in backs:
                await self._fire(m, "Welcome back.")
                fired += 1
        meetings = [m for m in active if m["kind"] == "meeting"]
        if meetings and self.calendar is not None and time.time() - self._meetings_at > 30:
            self._meetings_at = time.time()
            fired += await self._check_meetings(meetings)
        return fired

    async def _check_meetings(self, meetings: list[dict]) -> int:
        now = self.clock()
        window = SimpleNamespace(
            start=(now - timedelta(minutes=2)).isoformat(),
            end=(now + timedelta(minutes=1)).isoformat(),
        )
        try:
            events = (await asyncio.wait_for(self.calendar.events(window), 6))["events"]
        except Exception:
            return 0
        fired = 0
        for event in events:
            if event.get("all_day"):
                continue
            try:
                start = datetime.fromisoformat(event["start"])
            except (KeyError, ValueError):
                continue
            if not (now - timedelta(seconds=90) <= start <= now + timedelta(seconds=45)):
                continue
            title = event.get("title") or "Meeting"
            for m in meetings:
                key = f"{m['id']}:{event.get('id')}:{event['start']}"
                words = [w for w in m["target"].lower().split() if len(w) > 1]
                if key in self._met or not all(w in title.lower() for w in words):
                    continue
                self._met.add(key)
                await self._fire(m, f"“{title}” is starting.")
                fired += 1
        return fired

    async def run(self) -> None:
        """While Bridge is open: look every few seconds (less often with nothing to do)."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            busy = self.sense is not None and bool(self.store.all(active_only=True))
            await asyncio.sleep(TICK if busy else 15)
