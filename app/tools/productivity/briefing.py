"""One local briefing: today's schedule, due reminders, unread mail and the Mac's state.

Each source is optional. A missing permission or account skips that section instead of
failing the whole briefing.
"""

import asyncio
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from typing import Literal

from app.integrations.models import IntegrationError
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.productivity.calendar_mac import render_events
from app.tools.productivity.reminders import ReminderListInput


class BriefingInput(Input):
    day: Literal["today", "tomorrow"] = "today"


class BriefingController:
    def __init__(
        self,
        calendar=None,
        reminders=None,
        mac=None,
        gmail=None,
        clock=lambda: datetime.now().astimezone(),
    ):
        self.calendar, self.reminders, self.mac, self.gmail = calendar, reminders, mac, gmail
        self.clock = clock

    @staticmethod
    def _day(now: datetime, tomorrow: bool) -> tuple[datetime, datetime]:
        """The rest of today, or all of tomorrow, in local time."""
        if not tomorrow:
            return now, datetime.combine(now.date() + timedelta(days=1), time(), now.tzinfo)
        start = datetime.combine(now.date() + timedelta(days=1), time(), now.tzinfo)
        return start, start + timedelta(days=1)

    async def _schedule(self, now: datetime, tomorrow: bool = False):
        start, end = self._day(now, tomorrow)
        window = SimpleNamespace(start=start.isoformat(), end=end.isoformat())
        return (await self.calendar.events(window))["events"]

    async def _reminders(self, now: datetime, tomorrow: bool = False):
        listed = await self.reminders.list(ReminderListInput())
        last_day = (self._day(now, tomorrow)[0]).strftime("%Y-%m-%d")
        return [
            item for item in listed["reminders"] if item["due"] and item["due"][:10] <= last_day
        ]

    async def _mail(self, _now, _tomorrow=False):
        found = await self.gmail.search(
            SimpleNamespace(account_id=None, query="is:unread in:inbox newer_than:2d", limit=5)
        )
        return found["messages"]

    async def _mac(self, _now, _tomorrow=False):
        return await self.mac.status(None)

    async def brief(self, args=None):
        now = self.clock()
        tomorrow = getattr(args, "day", "today") == "tomorrow"
        sources = {
            "schedule": self._schedule if self.calendar else None,
            "reminders": self._reminders if self.reminders else None,
            "mail": self._mail if self.gmail else None,
            "mac": self._mac if self.mac and not tomorrow else None,
        }
        names = [name for name, source in sources.items() if source]
        results = await asyncio.gather(
            *(sources[name](now, tomorrow) for name in names), return_exceptions=True
        )
        sections, skipped = {}, {}
        for name, result in zip(names, results, strict=True):
            if isinstance(result, Exception):
                if name == "mail" and "No Gmail account" in str(result):
                    continue  # Gmail simply isn't set up; not worth mentioning daily.
                readable = isinstance(result, ValueError | IntegrationError)
                skipped[name] = str(result) if readable else "unavailable"
            else:
                sections[name] = result
        return {
            "tomorrow": tomorrow,
            "date": (now + timedelta(days=1 if tomorrow else 0)).strftime("%A %d %B"),
            "hour": now.hour,
            **sections,
            "skipped": skipped,
            "content_is_untrusted": True,
        }


def render(data: dict) -> str:
    greeting = (
        "Good morning"
        if data["hour"] < 12
        else "Good afternoon"
        if data["hour"] < 18
        else "Good evening"
    )
    tomorrow = data.get("tomorrow")
    heading = "Tomorrow:" if tomorrow else "Rest of today:"
    parts = [f"🌙 Tomorrow — {data['date']}" if tomorrow else f"☀️ {greeting} — {data['date']}"]
    if "schedule" in data:
        events = data["schedule"]
        parts.append(
            render_events({"events": events}).replace("Calendar:", heading)
            if events
            else f"{heading} nothing scheduled."
        )
    if "reminders" in data:
        due = data["reminders"]
        if due:
            parts.append("Reminders due:\n" + "\n".join(f"• {item['title']}" for item in due))
        else:
            parts.append("Reminders due: none.")
    if "mail" in data:
        mail = data["mail"]
        if mail:
            rows = [
                f"• {item['subject'] or '(no subject)'} — {item['from'].split('<')[0].strip()}"
                for item in mail
            ]
            parts.append("Unread email:\n" + "\n".join(rows))
        else:
            parts.append("Unread email: inbox zero. 🎉")
    if "mac" in data and data["mac"].get("battery_percent") is not None:
        mac = data["mac"]
        low = (
            " — plug in soon" if mac["battery_percent"] < 25 and not mac["on_power_adapter"] else ""
        )
        disk = " · Low disk space" if mac["disk_free_gb"] < 15 else ""
        parts.append(f"🔋 {mac['battery_percent']}%{low}{disk}")
    for name, reason in data["skipped"].items():
        parts.append(f"({name.capitalize()} skipped: {reason})")
    return "\n\n".join(parts)


def register(registry, controller):
    registry.register(
        Tool(
            "daily_briefing",
            "Give a briefing: the rest of today's (or all of tomorrow's) calendar, reminders "
            "due, unread email (if Gmail is connected) and battery. Use for 'brief me', "
            "'what's my day' or, with day=tomorrow, 'what's tomorrow look like'.",
            BriefingInput,
            RiskLevel.SAFE,
            controller.brief,
            render=render,
        )
    )
