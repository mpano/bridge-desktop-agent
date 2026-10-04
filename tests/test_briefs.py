"""Morning brief and evening wrap-up."""

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.assistant.briefs import Briefs, next_workday
from app.assistant.commitments import CommitmentStore, CommitmentTracker
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.workflows.watches import ProactiveStore

FRIDAY = datetime(2026, 10, 9, 9, 35).astimezone()


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Calendar:
    def __init__(self, events):
        self.events_by_day = events

    async def events(self, window):
        day = window.start[:10]
        return {"events": self.events_by_day.get(day, [])}


class LLM:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    async def complete(self, instructions, data):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer)


class Inbox:
    def __init__(self):
        self.last = (
            0,
            {
                "groups": {
                    "urgent": [
                        {
                            "message_id": "e1",
                            "from": "Sam Lee <sam@x.test>",
                            "subject": "Prod is down",
                            "summary": "Needs your OK to roll back",
                            "source": "gmail",
                        }
                    ],
                    "reply": [
                        {
                            "message_id": "e2",
                            "from": "Kim",
                            "subject": "Lunch?",
                            "summary": "Asks about lunch",
                            "source": "gmail",
                        }
                    ],
                }
            },
        )


def setup(tmp_path, clock, llm, events=None):
    store = CommitmentStore(tmp_path / "db", clock=lambda: clock().timestamp())
    tracker = CommitmentTracker(store, llm)
    proactive = ProactiveStore(tmp_path / "db")
    briefs = Briefs(
        tmp_path / "db",
        llm=llm,
        proactive_store=proactive,
        calendar=Calendar(events or {}),
        commitments=tracker,
        inbox=Inbox(),
        clock=clock,
    )
    return briefs, store, proactive


def promise(store, key, direction, person, what, due):
    store.add(
        {
            "message": key,
            "direction": direction,
            "person": person,
            "what": what,
            "due": due,
            "source": "email",
            "ref": {},
            "quote": "",
        }
    )
    return next(c for c in store.items(None) if c["what"] == what)


def test_morning_brief_picks_three_and_keeps_the_pick_for_the_day(tmp_path):
    clock = Clock(FRIDAY)
    day = FRIDAY.date().isoformat()
    events = {
        day: [
            {
                "title": "Standup",
                "start": FRIDAY.replace(hour=10).isoformat(),
                "end": FRIDAY.replace(hour=10, minute=15).isoformat(),
            }
        ]
    }
    llm = LLM()
    briefs, store, _ = setup(tmp_path, clock, llm, events)
    contract = promise(store, "a", "mine", "Olivier", "Send Olivier the contract", day)
    promise(store, "b", "mine", "Ana", "Review Ana's draft", "2026-10-20")  # Not due yet.
    promise(store, "c", "theirs", "Sam", "Send the numbers", "2026-10-07")
    llm.answers.append(
        {
            "top": [
                {"id": f"promise:{contract['id']}", "why": "You promised it today"},
                {"id": "inbox:e1", "why": "Sam is blocked on you"},
                {"id": "made-up", "why": "?"},
            ],
            "headline": "Two things matter most, then a quiet afternoon.",
        }
    )
    brief = asyncio.run(briefs.morning())
    assert [item["title"] for item in brief["top"]] == [
        "Send Olivier the contract",
        "Sam Lee: Needs your OK to roll back",
    ]
    assert brief["headline"].startswith("Two things")
    assert brief["meetings"]["count"] == 1 and brief["meetings"]["first"]["title"] == "Standup"
    assert [c["what"] for c in brief["due"]] == ["Send Olivier the contract"]
    assert [c["person"] for c in brief["waiting"]] == ["Sam"]
    assert brief["inbox"] == {"urgent": 1, "reply": 1}
    # Looking again doesn't ask the model again, and something done drops out of the 3.
    store.update(contract["id"], status="done")
    again = asyncio.run(briefs.morning())
    assert llm.calls == 1 and [item["id"] for item in again["top"]] == ["inbox:e1"]


def test_morning_brief_still_works_when_the_model_doesnt(tmp_path):
    briefs, store, _ = setup(tmp_path, Clock(FRIDAY), LLM(RuntimeError("down")))
    promise(store, "a", "mine", "Olivier", "Send Olivier the contract", "2026-10-08")
    brief = asyncio.run(briefs.morning())
    assert brief["top"][0]["title"] == "Send Olivier the contract"  # Overdue promises first.
    assert len(brief["top"]) == 3  # Then the email that needs you.


def test_evening_wrap_up_and_one_tap_to_monday(tmp_path):
    clock = Clock(FRIDAY.replace(hour=18, minute=5))
    monday = "2026-10-12"
    events = {monday: [{"title": "Planning", "start": "2026-10-12T09:30:00+02:00"}]}
    briefs, store, _ = setup(tmp_path, clock, LLM(), events)
    done = promise(store, "a", "mine", "Olivier", "Send Olivier the contract", "2026-10-09")
    store.update(done["id"], status="done", closed_by="you")
    late = promise(store, "b", "mine", "Ana", "Review Ana's draft", "2026-10-08")
    promise(store, "c", "mine", "Kim", "Book the venue", monday)
    brief = asyncio.run(briefs.evening())
    assert brief["headline"] == "1 done · 1 slipped · 1 meeting Monday"
    assert [c["what"] for c in brief["done"]] == ["Send Olivier the contract"]
    assert [c["what"] for c in brief["slipped"]] == ["Review Ana's draft"]
    assert (
        brief["tomorrow"]["name"] == "Monday"
        and brief["tomorrow"]["due"][0]["what"] == "Book the venue"
    )
    assert briefs.move_to_next_day([late["id"], "nope"]) == {"moved": 1, "to": monday}
    assert asyncio.run(briefs.evening())["slipped"] == []
    assert next_workday(FRIDAY.date()).isoformat() == monday


def test_on_time_once_on_weekdays_with_the_plan_made_first(tmp_path):
    clock = Clock(FRIDAY)
    llm = LLM({"top": [], "headline": "A calm Friday."})
    briefs, store, settings = setup(tmp_path, clock, llm)
    settings.update_settings(
        morning_plan=True, morning_time="09:30", evening_summary=True, evening_time="18:00"
    )
    planner = AsyncMock()
    briefs.day_planner = planner
    notes = []

    async def notify(title, body, view):
        notes.append((title, body, view))

    briefs.notify = notify
    asyncio.run(briefs.check())
    asyncio.run(briefs.check())
    # The model picked nothing, but someone needs you: those come first anyway.
    assert len(notes) == 1 and notes[0][0] == "Good morning — your 3 for today"
    assert notes[0][1].startswith("1. Sam Lee: Needs your OK to roll back")
    planner.plan.assert_awaited_once_with("today")
    promise(store, "b", "mine", "Ana", "Review Ana's draft", "2026-10-08")
    clock.now = FRIDAY.replace(hour=22)  # Four hours late: skip rather than surprise you.
    asyncio.run(briefs.check())
    clock.now = FRIDAY.replace(hour=18, minute=1)
    asyncio.run(briefs.check())
    assert notes[-1][0] == "Evening wrap-up"
    assert "Move “Review Ana's draft” to Monday?" in notes[-1][1]
    clock.now = FRIDAY + timedelta(days=1)  # Saturday.
    count = len(notes)
    asyncio.run(briefs.check())
    assert len(notes) == count


def test_brief_routes(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = agent.proactive = None
    briefs, store, _ = setup(tmp_path, Clock(FRIDAY.replace(hour=18)), LLM())
    agent.briefs = briefs
    late = promise(store, "b", "mine", "Ana", "Review Ana's draft", "2026-10-08")
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        brief = client.get("/api/v1/brief", headers=headers).json()
        assert brief["kind"] == "evening" and brief["slipped"][0]["id"] == late["id"]
        moved = client.post("/api/v1/brief/move", json={"ids": [late["id"]]}, headers=headers)
        assert moved.json() == {"moved": 1, "to": "2026-10-12"}
        assert (
            client.post("/api/v1/brief/move", json={"ids": []}, headers=headers).status_code == 422
        )
        assert client.get("/api/v1/brief").status_code == 401
