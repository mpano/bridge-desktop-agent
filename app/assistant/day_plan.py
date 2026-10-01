"""Plan my day: time blocks around meetings, from reminders and email that needs a reply.

The model proposes blocks; this module keeps only blocks that fit inside free time within
working hours and don't overlap each other. Nothing is added to the calendar until the
user approves the exact blocks.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from types import SimpleNamespace

from app.llm.structured import ask_json
from app.tools.productivity.reminders import ReminderListInput
from app.tools.productivity.timeslots import free_slots

MAX_BLOCKS = 12
KINDS = {"focus", "email", "task", "break", "admin"}
INSTRUCTIONS = """You plan the user's working day in time blocks. You get the date, the
current time, their meetings, the free time slots, reminders due, and emails that need a
reply. Propose blocks that fit ONLY inside the free slots: deep-work focus blocks of 60-120
minutes for important tasks, one or two short blocks to answer email, smaller task blocks
for reminders, and a lunch break around midday if there's room. Leave some slack; never
fill every minute. Titles are short and specific ("Reply to Olivier and Sarah", "Write API
docs"). Reply as JSON:
{"blocks": [{"start": "HH:MM", "end": "HH:MM", "title": "...", "kind":
"focus|email|task|break|admin", "why": "max 10 words"}], "note": "one short sentence"}"""


def clock(value: str) -> time:
    hours, _, minutes = value.partition(":")
    return time(int(hours), int(minutes))


class DayPlanner:
    def __init__(
        self,
        store,
        calendar,
        reminders=None,
        triage=None,
        llm=None,
        clock_fn=lambda: datetime.now().astimezone(),
    ):
        self.store, self.calendar, self.reminders = store, calendar, reminders
        self.triage, self.llm, self.now = triage, llm, clock_fn

    async def _inputs(self, day, start, end):
        window = SimpleNamespace(start=start.isoformat(), end=end.isoformat())
        jobs = [self.calendar.events(window)]
        jobs.append(
            self.reminders.list(ReminderListInput()) if self.reminders else asyncio.sleep(0)
        )
        jobs.append(self.triage.triage(days=2, limit=15) if self.triage else asyncio.sleep(0))
        events, reminders, inbox = await asyncio.gather(*jobs, return_exceptions=True)
        if isinstance(events, Exception):
            raise ValueError("I couldn't read your calendar. Allow Calendar access for Bridge.")
        due = []
        if isinstance(reminders, dict):
            last = day.strftime("%Y-%m-%d")
            due = [r["title"] for r in reminders["reminders"] if r["due"] and r["due"][:10] <= last]
        replies = []
        if isinstance(inbox, dict):
            for key in ("urgent", "reply"):
                replies += [
                    f"{item['from'].split('<')[0].strip()}: {item['summary']}"
                    for item in inbox["groups"][key]
                ]
        return events["events"], due, replies

    async def plan(self, which: str = "today") -> dict:
        settings = self.store.settings()
        now = self.now()
        day = now.date() + timedelta(days=1 if which == "tomorrow" else 0)
        zone = now.tzinfo
        work_start = datetime.combine(day, clock(settings["work_start"]), zone)
        work_end = datetime.combine(day, clock(settings["work_end"]), zone)
        if which == "today":
            # Start from the next quarter hour.
            soon = (now + timedelta(minutes=15 - now.minute % 15)).replace(second=0, microsecond=0)
            work_start = max(work_start, soon)
        if work_end - work_start < timedelta(minutes=30):
            raise ValueError("Your working day is over. Ask me to plan tomorrow instead.")
        events, due, replies = await self._inputs(day, work_start, work_end)
        busy = [
            (datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"]))
            for e in events
            if not e.get("all_day") and e.get("busy", True)
        ]
        free = free_slots(busy, work_start, work_end, 15)
        answer = await ask_json(
            self.llm,
            INSTRUCTIONS,
            {
                "date": day.strftime("%A %d %B %Y"),
                "now": now.strftime("%H:%M"),
                "working_hours": f"{settings['work_start']}-{settings['work_end']}",
                "meetings": [
                    {"title": e["title"], "start": e["start"][11:16], "end": e["end"][11:16]}
                    for e in events
                    if not e.get("all_day")
                ],
                "free_slots": [f"{a.strftime('%H:%M')}-{b.strftime('%H:%M')}" for a, b in free],
                "reminders_due": due[:20],
                "emails_needing_reply": replies[:12],
            },
        )
        blocks = self.validate(answer, day, zone, free)
        plan_id = self.store.save_plan(day.isoformat(), blocks)
        return {
            "plan_id": plan_id,
            "day": day.strftime("%A %d %B"),
            "meetings": [
                {"title": e["title"], "start": e["start"], "end": e["end"]}
                for e in events
                if not e.get("all_day")
            ],
            "blocks": blocks,
            "note": str(answer.get("note", ""))[:200] if isinstance(answer, dict) else "",
        }

    @staticmethod
    def validate(answer, day, zone, free) -> list[dict]:
        blocks, taken = [], []
        for item in (answer.get("blocks", []) if isinstance(answer, dict) else [])[:30]:
            try:
                start = datetime.combine(day, clock(str(item["start"])), zone)
                end = datetime.combine(day, clock(str(item["end"])), zone)
            except (KeyError, ValueError, TypeError):
                continue
            if end - start < timedelta(minutes=10):
                continue
            if not any(a <= start and end <= b for a, b in free):
                continue  # Overlaps a meeting or falls outside working hours.
            if any(start < b and a < end for a, b in taken):
                continue
            taken.append((start, end))
            blocks.append(
                {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "title": " ".join(str(item.get("title", "Focus time")).split())[:80],
                    "kind": item.get("kind") if item.get("kind") in KINDS else "task",
                    "why": " ".join(str(item.get("why", "")).split())[:80],
                }
            )
            if len(blocks) >= MAX_BLOCKS:
                break
        return sorted(blocks, key=lambda block: block["start"])

    async def apply(self, blocks: list[dict]) -> dict:
        created = []
        for block in blocks:
            view = await asyncio.to_thread(
                self.calendar.backend.create,
                block["title"],
                datetime.fromisoformat(block["start"]),
                datetime.fromisoformat(block["end"]),
                None,
                "",
                f"Planned by Bridge. {block.get('why', '')}".strip(),
            )
            created.append(view["title"])
        return {"created": created, "count": len(created)}


ICONS = {"focus": "🎯", "email": "✉️", "task": "✅", "break": "☕", "admin": "🗂"}


def render(data: dict) -> str:
    lines = [f"🗓 Plan for {data['day']}"]
    entries = [(m["start"], "📅", m["title"], "", m["end"]) for m in data["meetings"]] + [
        (b["start"], ICONS.get(b["kind"], "•"), b["title"], b["why"], b["end"])
        for b in data["blocks"]
    ]
    for start, icon, title, why, end in sorted(entries):
        span = f"{start[11:16]}–{end[11:16]}"
        lines.append(f"{span}  {icon} {title}" + (f"  · {why}" if why else ""))
    if not data["blocks"]:
        lines.append("No free time to plan — your calendar is full.")
    if data.get("note"):
        lines.append(f"\n{data['note']}")
    if data["blocks"]:
        lines.append("\nSay “add it to my calendar” to block this time.")
    return "\n".join(lines)
