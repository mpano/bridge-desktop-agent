"""Morning brief and evening wrap-up: the chief-of-staff bookends of your day.

Morning: the three things that matter today (picked from promises due, email and Slack that
need you, and meetings), plus what's due and who you're waiting on. Evening: what got done,
what slipped (one tap moves it to tomorrow), and tomorrow at a glance.

Sections are worked out live from Bridge's own data each time you look, so they never go
stale; only the morning's "top 3" (a model's pick) is kept for the day. What reaches the
model is the same short titles and summaries Bridge already made, never whole messages.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.llm.structured import ask_json

log = logging.getLogger(__name__)
TOP = 3
PICK = """You are the user's chief of staff. From the candidates below, pick the three
things that matter most for the user today, in order. Prefer: promises the user made that are
due or overdue, people blocked waiting on the user (including code reviews they asked for),
urgent email or Slack, Jira issues due today, and meetings that need preparation. Skip
routine things. For each give "why": at most 10 words saying why it matters today
(e.g. "You promised Olivier it today", "Sam is blocked on your answer").
Also write "headline": one short, warm sentence summing up the day (at most 14 words, no
exclamation marks). Reply as JSON:
{"top": [{"id": "<candidate id>", "why": "..."}], "headline": "..."}"""


def next_workday(day: date) -> date:
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


class Briefs:
    def __init__(
        self,
        path: Path | str,
        *,
        llm,
        proactive_store=None,
        calendar=None,
        commitments=None,
        inbox=None,
        slack_inbox=None,
        day_planner=None,
        clock=None,
    ):
        self.path, self.llm = str(path), llm
        self.settings_store, self.calendar = proactive_store, calendar
        self.commitments, self.inbox, self.slack = commitments, inbox, slack_inbox
        self.day_planner = day_planner
        self.jira = self.github = None  # Work tools, when connected.
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.notify = None
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS briefs (day TEXT NOT NULL, kind TEXT NOT NULL, "
                "data TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (day, kind))"
            )

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    def _saved(self, day: str, kind: str) -> dict | None:
        with self._db() as db:
            row = db.execute(
                "SELECT data, created FROM briefs WHERE day = ? AND kind = ?", (day, kind)
            ).fetchone()
        return {**json.loads(row[0]), "created": row[1]} if row else None

    def _save(self, day: str, kind: str, data: dict) -> None:
        with self._db() as db:
            db.execute(
                "DELETE FROM briefs WHERE day < ?",
                ((self.clock().date() - timedelta(days=14)).isoformat(),),
            )
            db.execute(
                "INSERT INTO briefs VALUES (?, ?, ?, ?) ON CONFLICT(day, kind) DO UPDATE "
                "SET data = excluded.data, created = excluded.created",
                (day, kind, json.dumps(data), self.clock().timestamp()),
            )

    # What Bridge knows right now -------------------------------------------------------------

    async def _events(self, day: date) -> list[dict] | None:
        if self.calendar is None:
            return None
        start = datetime.combine(day, datetime.min.time()).astimezone()
        window = SimpleNamespace(
            start=start.isoformat(), end=(start + timedelta(days=1)).isoformat()
        )
        try:
            found = (await self.calendar.events(window))["events"]
        except Exception:
            return None
        return [
            {"title": e.get("title") or "Busy", "start": e["start"], "end": e.get("end")}
            for e in found
            if not e.get("all_day")
        ]

    def _promises(self, status: str = "open", since: float = 0.0) -> list[dict]:
        if self.commitments is None:
            return []
        return self.commitments.store.items(status, since=since)

    def _needs_you(self) -> list[dict]:
        items = []
        for source in (self.inbox, self.slack):
            last = getattr(source, "last", None) if source is not None else None
            for group in ("urgent", "reply"):
                for item in last[1]["groups"].get(group, []) if last else []:
                    items.append(
                        {
                            "id": f"inbox:{item['message_id']}",
                            "group": group,
                            "source": item.get("source", "gmail"),
                            "from": item["from"].split("<")[0].strip().strip('"') or item["from"],
                            "subject": item.get("subject") or "",
                            "summary": item.get("summary") or "",
                        }
                    )
        return items

    @staticmethod
    def _promise_view(item: dict) -> dict:
        return {key: item[key] for key in ("id", "direction", "person", "what", "due", "source")}

    def _meetings(self, events: list[dict] | None, after: datetime | None = None) -> dict:
        if events is None:
            return {"count": None, "first": None}
        upcoming = sorted(
            (e for e in events if not after or datetime.fromisoformat(e["start"]) >= after),
            key=lambda e: e["start"],
        )
        return {"count": len(events), "first": upcoming[0] if upcoming else None}

    # Morning ---------------------------------------------------------------------------------

    async def morning(self, *, refresh: bool = False) -> dict:
        now = self.clock()
        today = now.date()
        events = await self._events(today)
        promises = self._promises()
        due = [
            c
            for c in promises
            if c["direction"] == "mine" and c["due"] and c["due"] <= today.isoformat()
        ]
        waiting = [
            c
            for c in promises
            if c["direction"] == "theirs" and c["due"] and c["due"] < today.isoformat()
        ]
        needs = self._needs_you()
        candidates = {}
        for c in due:
            candidates[f"promise:{c['id']}"] = {
                "kind": "promise",
                "title": c["what"],
                "detail": f"You promised {c['person']}, due {c['due']}",
                "ref": {"id": c["id"]},
            }
        for c in waiting:
            candidates[f"promise:{c['id']}"] = {
                "kind": "waiting",
                "title": f"{c['person']}: {c['what']}",
                "detail": f"They promised it by {c['due']}; follow up?",
                "ref": {"id": c["id"]},
            }
        for item in needs:
            candidates[item["id"]] = {
                "kind": "email" if item["source"] != "slack" else "slack",
                "title": f"{item['from']}: {item['summary'] or item['subject']}"[:140],
                "detail": "Urgent" if item["group"] == "urgent" else "Waiting for your reply",
                "ref": {"message_id": item["id"].split(":", 1)[1]},
            }
        for key, item in (await self._work(today)).items():
            candidates[key] = item
        for event in (events or [])[:8]:
            start = datetime.fromisoformat(event["start"])
            if start < now:
                continue
            candidates[f"meeting:{event['start']}:{event['title']}"[:120]] = {
                "kind": "meeting",
                "title": event["title"],
                "detail": f"Meeting at {start.strftime('%H:%M')}",
                "ref": {"start": event["start"]},
            }
        saved = self._saved(today.isoformat(), "morning")
        if refresh or saved is None:
            saved = await self._pick(today, candidates)
            self._save(today.isoformat(), "morning", saved)
        top = [
            {**candidates[entry["id"]], "id": entry["id"], "why": entry["why"]}
            for entry in saved.get("top", [])
            if entry["id"] in candidates  # Done since this morning: drop it from the 3.
        ]
        counts = {"urgent": 0, "reply": 0}
        for item in needs:
            counts[item["group"]] += 1
        return {
            "kind": "morning",
            "day": today.isoformat(),
            "headline": saved.get("headline") or "",
            "made_at": saved.get("created"),
            "top": top,
            "meetings": self._meetings(events, after=now),
            "due": [self._promise_view(c) for c in due],
            "waiting": [self._promise_view(c) for c in waiting],
            "inbox": counts,
        }

    async def _work(self, today: date) -> dict:
        """Pull requests waiting for your review, and Jira issues due or most urgent."""
        found: dict = {}
        try:
            if self.github is not None and await self.github.connected():
                for pr in (await self.github.reviews())[:4]:
                    found[f"review:{pr['repo']}#{pr['number']}"] = {
                        "kind": "review",
                        "title": f"Review: {pr['title']}",
                        "detail": f"{pr['author'] or 'Someone'} asked you to review it",
                        "ref": {"url": pr["url"]},
                    }
        except Exception:
            log.warning("Morning brief: couldn't read GitHub.")
        try:
            if self.jira is not None and await self.jira.connected():
                for issue in await self.jira.mine():
                    due = issue.get("due") and issue["due"] <= today.isoformat()
                    urgent = issue.get("priority") in {"Highest", "High", "Blocker", "Critical"}
                    if not due and not urgent:
                        continue
                    found[f"ticket:{issue['key']}"] = {
                        "kind": "ticket",
                        "title": f"{issue['key']} {issue['summary']}",
                        "detail": f"Due {issue['due']}" if due else f"{issue['priority']} priority",
                        "ref": {"url": issue["url"]},
                    }
                    if len(found) >= 8:
                        break
        except Exception:
            log.warning("Morning brief: couldn't read Jira.")
        return found

    async def _pick(self, today: date, candidates: dict) -> dict:
        if not candidates:
            return {
                "top": [],
                "headline": "A clear day: nothing is due and nobody is waiting on you.",
            }
        order = sorted(
            candidates,
            key=lambda key: (
                {
                    "promise": 0,
                    "email": 1,
                    "slack": 1,
                    "review": 2,
                    "ticket": 2,
                    "waiting": 3,
                    "meeting": 4,
                }[candidates[key]["kind"]],
                key,
            ),
        )
        fallback = {
            "top": [{"id": key, "why": candidates[key]["detail"]} for key in order[:TOP]],
            "headline": "",
        }
        try:
            answer = await ask_json(
                self.llm,
                PICK,
                {
                    "today": today.isoformat(),
                    "candidates": [
                        {"id": key, "kind": c["kind"], "title": c["title"], "detail": c["detail"]}
                        for key, c in candidates.items()
                    ][:40],
                },
            )
        except Exception:
            log.warning("Morning brief: couldn't pick the top 3; using due dates instead.")
            return fallback
        if not isinstance(answer, dict):
            return fallback
        top, seen = [], set()
        for entry in answer.get("top") or []:
            key = str((entry or {}).get("id", ""))
            if key in candidates and key not in seen:
                seen.add(key)
                top.append({"id": key, "why": " ".join(str(entry.get("why") or "").split())[:90]})
        return {
            "top": top[:TOP] or fallback["top"],
            "headline": " ".join(str(answer.get("headline") or "").split())[:140],
        }

    # Evening ---------------------------------------------------------------------------------

    async def evening(self) -> dict:
        now = self.clock()
        today = now.date()
        tomorrow = next_workday(today)
        start = datetime.combine(today, datetime.min.time()).astimezone().timestamp()
        promises = self._promises()
        done = [c for c in self._promises("done", since=start) if c["updated"] >= start]
        slipped = [
            c
            for c in promises
            if c["direction"] == "mine" and c["due"] and c["due"] <= today.isoformat()
        ]
        due_next = [
            c for c in promises if c["direction"] == "mine" and c["due"] == tomorrow.isoformat()
        ]
        waiting = [
            c
            for c in promises
            if c["direction"] == "theirs" and c["due"] and c["due"] <= today.isoformat()
        ]
        counts = {"urgent": 0, "reply": 0}
        for item in self._needs_you():
            counts[item["group"]] += 1
        events = await self._events(tomorrow)
        parts = [f"{len(done)} done"] if done else []
        if slipped:
            parts.append(f"{len(slipped)} slipped")
        if events:
            plural = "s" if len(events) != 1 else ""
            parts.append(f"{len(events)} meeting{plural} {self.day_name(tomorrow, today)}")
        return {
            "kind": "evening",
            "day": today.isoformat(),
            "headline": " · ".join(parts) or "A quiet day. Nothing slipped.",
            "done": [self._promise_view(c) | {"closed_by": c["closed_by"]} for c in done],
            "slipped": [self._promise_view(c) for c in slipped],
            "tomorrow": {
                "day": tomorrow.isoformat(),
                "name": self.day_name(tomorrow, today),
                "meetings": self._meetings(events),
                "due": [self._promise_view(c) for c in due_next],
            },
            "waiting": [self._promise_view(c) for c in waiting],
            "inbox": counts,
        }

    @staticmethod
    def day_name(day: date, today: date) -> str:
        return "tomorrow" if day == today + timedelta(days=1) else day.strftime("%A")

    def move_to_next_day(self, ids: list[str]) -> dict:
        """One tap: slipped promises move to the next working day."""
        if self.commitments is None:
            return {"moved": 0}
        day = next_workday(self.clock().date()).isoformat()
        moved = 0
        for commitment_id in ids:
            item = self.commitments.store.get(commitment_id)
            if item and item["status"] == "open" and item["direction"] == "mine":
                moved += self.commitments.store.update(commitment_id, due=day, nudged=None)
        return {"moved": moved, "to": day}

    # Which one Today shows -------------------------------------------------------------------

    def current_kind(self) -> str:
        now = self.clock()
        settings = self.settings_store.settings() if self.settings_store else {}
        hour, minute = hhmm(settings.get("evening_time", "18:00"))
        evening_from = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return "evening" if now >= evening_from - timedelta(hours=1) else "morning"

    async def current(self, *, refresh: bool = False) -> dict:
        return (
            await self.evening()
            if self.current_kind() == "evening"
            else await self.morning(refresh=refresh)
        )

    # On time, on weekdays (called by the proactive loop) -----------------------------------

    async def check(self) -> None:
        if self.settings_store is None or self.notify is None:
            return
        now = self.clock()
        if now.weekday() >= 5:
            return
        settings = self.settings_store.settings()
        today = now.date().isoformat()
        for kind, enabled_key in (("morning", "morning_plan"), ("evening", "evening_summary")):
            if not settings.get(enabled_key):
                continue
            hour, minute = hhmm(settings[f"{kind}_time"])
            at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # On time, or late if the Mac was asleep, but never hours late.
            if not (at <= now < at + timedelta(hours=3)):
                continue
            if not self.settings_store.notify_once(f"brief:{kind}:{today}"):
                continue
            try:
                await (self._send_morning() if kind == "morning" else self._send_evening())
            except Exception:
                log.warning("The %s brief failed.", kind, exc_info=True)

    async def _send_morning(self) -> None:
        if (
            self.day_planner is not None
            and self.settings_store.latest_plan(self.clock().date().isoformat()) is None
        ):
            try:
                await self.day_planner.plan("today")  # As the old "Plan my day" routine did.
            except Exception:
                log.warning("Morning brief: the day plan failed.")
        brief = await self.morning(refresh=True)
        if brief["top"]:
            body = " · ".join(f"{i}. {item['title']}" for i, item in enumerate(brief["top"], 1))
        else:
            body = brief["headline"]
        await self.notify("Good morning — your 3 for today", body, "today")

    async def _send_evening(self) -> None:
        brief = await self.evening()
        body = brief["headline"]
        if brief["slipped"]:
            first = brief["slipped"][0]["what"]
            body += f". Move “{first}” to {brief['tomorrow']['name']}?"
        await self.notify("Evening wrap-up", body, "today")
