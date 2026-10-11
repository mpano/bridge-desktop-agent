"""Bridge on your desk: what the avatar in the corner of your screen shows.

Your next meeting (with a countdown when it's close), what needs you (approvals, recipe runs,
the weekly report, overdue promises), the latest notifications (the avatar pops up a bubble
when one arrives), your Bridge voice's colors, and whether it's after hours (it sleeps).
Everything here stays on the Mac; nothing is sent anywhere.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from datetime import date, datetime, timedelta
from types import SimpleNamespace

CALENDAR_SECONDS = 60
MEETING_HOURS = 12


class Buddy:
    def __init__(self, agent, settings, clock=None):
        self.agent, self.settings = agent, settings
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.feed: deque[dict] = deque(maxlen=20)
        self._ids = itertools.count(1)
        self._calendar: tuple[float, list] | None = None

    def heard(self, title: str, message: str, view: str = "today") -> None:
        """A notification went out: the avatar reacts to it."""
        self.feed.append(
            {
                "id": next(self._ids),
                "title": " ".join(title.split())[:100],
                "body": " ".join(message.split())[:200],
                "view": view,
                "at": time.time(),
            }
        )

    async def _events(self) -> list[dict]:
        calendar = getattr(self.agent, "calendar", None)
        if calendar is None:
            return []
        if self._calendar and time.time() - self._calendar[0] < CALENDAR_SECONDS:
            return self._calendar[1]
        now = self.clock()
        window = SimpleNamespace(
            start=now.isoformat(), end=(now + timedelta(hours=MEETING_HOURS)).isoformat()
        )
        try:
            found = (await asyncio.wait_for(calendar.events(window), 5))["events"]
        except Exception:
            found = []
        self._calendar = (time.time(), found)
        return found

    async def next_meeting(self) -> dict | None:
        now = self.clock()
        upcoming = []
        for event in await self._events():
            if event.get("all_day"):
                continue
            try:
                start = datetime.fromisoformat(event["start"])
                end = datetime.fromisoformat(event.get("end") or event["start"])
            except (KeyError, ValueError):
                continue
            if end > now:
                upcoming.append((start, end, event))
        if not upcoming:
            return None
        start, end, event = min(upcoming, key=lambda item: item[0])
        minutes = round((start - now).total_seconds() / 60)
        return {
            "title": event.get("title") or "Meeting",
            "start": start.isoformat(),
            "minutes": minutes,
            "now": minutes <= 0,
            "location": event.get("location") or "",
        }

    def needs(self) -> list[dict]:
        found = []
        agent = self.agent
        try:
            approvals = len(agent.pending_approvals())
        except Exception:
            approvals = 0
        if approvals:
            found.append(
                {
                    "kind": "approvals",
                    "count": approvals,
                    "view": "today",
                    "label": f"{approvals} approval{'s' if approvals > 1 else ''} waiting",
                }
            )
        recipes = getattr(agent, "recipes", None)
        if recipes is not None:
            waiting = len(recipes.runs("waiting"))
            if waiting:
                found.append(
                    {
                        "kind": "recipes",
                        "count": waiting,
                        "view": "today",
                        "label": f"{waiting} recipe{'s' if waiting > 1 else ''} want your OK",
                    }
                )
        reports = getattr(agent, "reports", None)
        if reports is not None:
            try:
                report = reports.view()
            except Exception:
                report = None
            if report and report["due"] and report["text"] and not report["sent"]:
                found.append(
                    {
                        "kind": "report",
                        "count": 1,
                        "view": "today",
                        "label": "Your weekly report is ready",
                    }
                )
        commitments = getattr(agent, "commitments", None)
        if commitments is not None:
            today = date.today().isoformat()
            late = [
                item
                for item in commitments.store.items("open")
                if item["direction"] == "mine" and item.get("due") and item["due"] < today
            ]
            if late:
                found.append(
                    {
                        "kind": "promises",
                        "count": len(late),
                        "view": "today",
                        "label": f"{len(late)} promise{'s' if len(late) > 1 else ''} overdue",
                    }
                )
        return found

    def sleeping(self) -> bool:
        """After hours and at weekends the avatar dozes (it still wakes for notifications)."""
        now = self.clock()
        if now.weekday() >= 5:
            return True
        store = getattr(self.agent, "proactive_store", None)
        hours = store.settings() if store is not None else {}
        start, end = hours.get("work_start", "08:00"), hours.get("work_end", "19:00")
        current = now.strftime("%H:%M")
        return not (start <= current < end)

    def voice(self) -> dict:
        talk = getattr(self.agent, "talk", None)
        if talk is None:
            return {"name": "Bridge", "colors": ["#6f7dff", "#b48cff"]}
        current = next((v for v in talk.voices() if v["current"]), None)
        return {
            "name": current["name"] if current else "Bridge",
            "colors": current["colors"] if current else ["#6f7dff", "#b48cff"],
        }

    async def state(self) -> dict:
        needs = self.needs()
        meetings = getattr(self.agent, "meetings", None)
        recording = getattr(meetings, "current", None) if meetings is not None else None
        return {
            "recording": recording["title"] if recording else None,
            "notes": bool(meetings is not None and meetings.enabled and meetings.audio_factory),
            "enabled": bool(getattr(self.settings, "desk_buddy", True)),
            "talk": bool(getattr(self.settings, "talk_enabled", False)),
            "voice": self.voice(),
            "sleeping": self.sleeping(),
            "meeting": await self.next_meeting(),
            "needs": needs,
            "count": sum(item["count"] for item in needs),
            "latest": list(self.feed)[-5:],
        }
