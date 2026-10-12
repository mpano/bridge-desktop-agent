"""People: who's who from Bridge's own data, profiles, meeting briefs. All fake data."""

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.assistant.commitments import CommitmentStore
from app.assistant.meetings import MeetingStore
from app.assistant.people import People, split_from

ZONE = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 13, 13, 50, tzinfo=ZONE)
EVENT = {
    "id": "ev1",
    "title": "Design review",
    "start": (NOW + timedelta(minutes=10)).isoformat(),
    "end": (NOW + timedelta(minutes=40)).isoformat(),
    "attendees": [
        {"name": "Sam Lee", "email": "sam@acme.co"},
        {"name": "Ana Ruiz", "email": "ana@acme.co"},
    ],
}


def promise(store, direction, person, what, due=None, message="m1"):
    store.add(
        {
            "message": message,
            "direction": direction,
            "person": person,
            "what": what,
            "due": due,
            "source": "email",
            "ref": {},
            "quote": what,
        }
    )


def world(tmp_path):
    commitments = CommitmentStore(tmp_path / "db")
    promise(commitments, "theirs", "Sam", "Fix the login bug", "2026-10-16", "m1")
    promise(commitments, "mine", "Ana Ruiz", "Send the deck", None, "m2")
    promise(commitments, "mine", "me", "Buy milk", None, "m3")
    meetings = MeetingStore(tmp_path / "db")
    meetings.save(
        {
            "id": "meet1",
            "title": "Weekly sync",
            "event": {"attendees": [{"name": "Sam Lee", "email": "sam@acme.co"}]},
            "started": time.time() - 86400,
            "ended": time.time() - 84000,
            "status": "done",
            "segments": [],
            "notes": [],
            "summary": {
                "summary": "Agreed to ship Friday.",
                "actions": [{"owner": "them", "person": "Sam", "what": "Write the release notes"}],
            },
            "sent": [],
            "promises": [],
        }
    )
    inbox = SimpleNamespace(
        last=(
            time.time(),
            {
                "groups": {
                    "reply": [
                        {
                            "from": "Sam Lee <SAM@acme.co>",
                            "subject": "Login bug",
                            "summary": "Found the cause, fix tomorrow",
                            "date": "",
                        }
                    ],
                    "newsletter": [
                        {
                            "from": "Weekly Digest <news@spam.co>",
                            "subject": "Deals",
                            "summary": "",
                            "date": "",
                        }
                    ],
                    "fyi": [
                        {
                            "from": "Olivier Mupenzi <olivier@acme.co>",
                            "subject": "Budget",
                            "summary": "Numbers attached",
                            "date": "",
                        },
                        {
                            "from": "Me <me@acme.co>",
                            "subject": "Note to self",
                            "summary": "",
                            "date": "",
                        },
                    ],
                }
            },
        )
    )
    calendar = SimpleNamespace(events=AsyncMock(return_value={"events": [EVENT]}))
    github = SimpleNamespace(
        connected=AsyncMock(return_value=True),
        reviews=AsyncMock(
            return_value=[
                {
                    "author": "samlee",
                    "title": "Faster sync",
                    "repo": "acme/app",
                    "url": "https://gh/412",
                    "number": 412,
                }
            ]
        ),
    )
    agent = SimpleNamespace(
        commitments=SimpleNamespace(store=commitments),
        meetings=SimpleNamespace(store=meetings),
        inbox=inbox,
        slack_inbox=None,
        calendar=calendar,
        github=github,
    )
    llm = SimpleNamespace(complete=AsyncMock())
    people = People(
        tmp_path / "db", llm=llm, agent=agent, me=lambda: ("Mpano", "me@acme.co"), clock=lambda: NOW
    )
    return people, llm


def by_name(people):
    return {p["name"]: p for p in people.store.all()}


async def test_learns_who_is_who_from_bridges_own_data(tmp_path):
    people, _ = world(tmp_path)
    await people.collect(force=True)
    known = by_name(people)
    assert set(known) == {"Sam Lee", "Ana Ruiz", "Olivier Mupenzi", "samlee"}
    sam = known["Sam Lee"]
    assert sam["emails"] == ["sam@acme.co"]  # One person from promises, meetings, mail, calendar.
    assert set(sam["seen"]) >= {"promises", "meetings", "email", "calendar"}
    # Not you, not a newsletter, not "me".
    assert not any("digest" in n.lower() or n in {"Me", "me", "Mpano"} for n in known)
    assert split_from("Olivier <O@X.co>") == ("Olivier", "o@x.co")
    from app.assistant.people import automated

    for sender in (
        "GitHub <noreply@github.com>",
        "Jira <jira@acme.atlassian.net>",
        "Calendar <calendar-notification@google.com>",
        "Bot <no-reply+x@acme.co>",
    ):
        assert automated(*split_from(sender)), sender
    assert not automated(*split_from("Sam Lee <sam@acme.co>"))

    merged = people.merge(sam["id"], known["samlee"]["id"])
    assert merged["handles"] == ["samlee"] and "samlee" not in by_name(people)


async def test_a_profile_says_whats_open_both_ways(tmp_path):
    people, llm = world(tmp_path)
    await people.collect(force=True)
    known = by_name(people)
    people.merge(known["Sam Lee"]["id"], known["samlee"]["id"])
    profile = await people.profile(known["Sam Lee"]["id"])
    f = profile["facts"]
    assert [i["what"] for i in f["open"]["they_owe"]] == ["Fix the login bug"]
    assert f["open"]["you_owe"] == []
    assert f["meetings"][0]["title"] == "Weekly sync"
    assert f["meetings"][0]["their_actions"] == ["Write the release notes"]
    assert f["messages"][0]["subject"] == "Login bug"
    assert f["upcoming"][0]["title"] == "Design review"
    assert f["reviews"][0]["title"] == "Faster sync"
    ana = await people.profile(known["Ana Ruiz"]["id"])
    assert [i["what"] for i in ana["facts"]["open"]["you_owe"]] == ["Send the deck"]

    llm.complete.return_value = json.dumps(
        {"summary": "You're shipping onboarding together.", "next": "Ask about the fix."}
    )
    summary = await people.together(known["Sam Lee"]["id"])
    assert summary["summary"] == "You're shipping onboarding together."
    sent = llm.complete.await_args.args[1]
    assert "https://gh/412" not in sent and "Fix the login bug (due 2026-10-16)" in sent
    await people.together(known["Sam Lee"]["id"])
    assert llm.complete.await_count == 1  # Kept for a few hours…
    people.note(known["Sam Lee"]["id"], "Prefers Slack")
    await people.together(known["Sam Lee"]["id"])
    assert llm.complete.await_count == 2  # …unless your note changed.
    assert "Prefers Slack" in llm.complete.await_args.args[1]


async def test_a_brief_before_the_meeting(tmp_path):
    people, llm = world(tmp_path)
    llm.complete.return_value = json.dumps(
        {"brief": ["Sam still owes you the login fix (due Friday)"]}
    )
    brief = await people.brief(EVENT)
    assert brief["brief"] == ["Sam still owes you the login fix (due Friday)"]
    assert {p["name"] for p in brief["people"]} == {"Sam Lee", "Ana Ruiz"}
    assert (await people.next_brief())["event"]["title"] == "Design review"
    assert llm.complete.await_count == 1  # Once per meeting.

    people.llm = None
    people._briefs.clear()
    plain = await people.brief(EVENT)
    assert "Sam Lee owes you: Fix the login bug" in plain["brief"]
    assert "You owe Ana Ruiz: Send the deck" in plain["brief"]


async def test_forgotten_people_stay_forgotten(tmp_path):
    people, _ = world(tmp_path)
    await people.collect(force=True)
    olivier = by_name(people)["Olivier Mupenzi"]
    people.note(olivier["id"], "Budget owner")
    assert people.forget(olivier["id"]) == {"forgotten": "Olivier Mupenzi"}
    await people.collect(force=True)
    assert "Olivier Mupenzi" not in by_name(people)
    hidden = [p for p in people.store.all(hidden=True) if p["hidden"]][0]
    assert hidden["note"] == "" and hidden["seen"] == {}


async def test_the_meeting_heads_up_includes_the_brief(tmp_path):
    from app.agent.proactive import Proactive
    from app.workflows.watches import ProactiveStore

    people, llm = world(tmp_path)
    llm.complete.return_value = json.dumps({"brief": ["Sam owes you the login fix"]})
    notify = AsyncMock()
    engine = Proactive(
        ProactiveStore(tmp_path / "db"), notify, calendar=people.agent.calendar, clock=lambda: NOW
    )
    engine.people = people
    assert await engine.check_meetings() == 1
    title, body = notify.await_args.args
    assert title == "In 10 min: Design review" and body.startswith("Sam owes you the login fix (")


def test_people_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.bootstrap import build_agent
    from app.config.settings import Settings
    from app.phone.access import phone_may_use

    monkeypatch.chdir(tmp_path)
    config = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    agent = build_agent(config, llm=AsyncMock())
    promise(agent.commitments.store, "theirs", "Dana Kim", "Review the plan")
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(config, agent, enable_ui=True)) as client:
        listed = client.get("/api/v1/people", headers=headers).json()["people"]
        assert [p["name"] for p in listed] == ["Dana Kim"]
        dana = listed[0]["id"]
        profile = client.get(f"/api/v1/people/profile?id={dana}", headers=headers).json()
        assert profile["facts"]["open"]["they_owe"][0]["what"] == "Review the plan"
        assert (
            client.post(
                "/api/v1/people/note", json={"id": dana, "note": "Design lead"}, headers=headers
            ).json()["note"]
            == "Design lead"
        )
        assert client.get("/api/v1/people/profile?id=abcdef12", headers=headers).status_code == 404
        assert (
            client.post("/api/v1/people/forget", json={"id": dana}, headers=headers).status_code
            == 200
        )
        assert client.get("/api/v1/people", headers=headers).json()["people"] == []
        assert client.get("/api/v1/people/brief", headers=headers).json() == {"brief": None}
        assert "BridgePeople" in client.get("/ui/people.js").text
        assert client.get("/api/v1/people").status_code == 401
    assert phone_may_use("GET", "/api/v1/people/profile")
    assert not phone_may_use("POST", "/api/v1/people/forget")
    names = [t.name for t in agent.executor.registry.tools()]
    assert {"person_lookup", "meeting_prep", "person_note"} <= set(names)


@pytest.mark.parametrize("value,expected", [("Sam", True), ("sam lee", True), ("Samuel", False)])
def test_first_names_match_a_full_name(tmp_path, value, expected):
    people = People(tmp_path / "db")
    person = {"name": "Sam Lee", "emails": [], "handles": []}
    assert people.match(person, name=value) is expected
