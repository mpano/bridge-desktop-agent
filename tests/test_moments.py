"""Reminders for a moment. A fake Mac (front app, idle time) and a fake calendar."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.assistant.describe import kind_of
from app.assistant.moments import AWAY_SECONDS, Moments, understand_rules

ZONE = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 14, 9, 30, 20, tzinfo=ZONE)


@pytest.mark.parametrize(
    "words,kind,target,text",
    [
        (
            "remind me to ask about the budget when the standup starts",
            "meeting",
            "standup",
            "Ask about the budget",
        ),
        (
            "When I open Figma, remind me to check Ana's feedback",
            "app",
            "Figma",
            "Check Ana's feedback",
        ),
        ("remind me to call the bank when I'm back at my desk", "back", "", "Call the bank"),
        (
            "remind me to share my screen when I join the design review",
            "meeting",
            "design review",
            "Share my screen",
        ),
        ("remind me to drink water when a meeting starts", "meeting", "", "Drink water"),
    ],
)
def test_plain_words(words, kind, target, text):
    assert understand_rules(words) == {"kind": kind, "target": target, "text": text}


class FakeMac:
    def __init__(self):
        self.front = {"app": "Mail", "bundle_id": "com.apple.mail"}
        self.idle = 1.0

    def frontmost(self):
        return self.front

    def idle_seconds(self):
        return self.idle


def calendar():
    event = {
        "id": "e1",
        "title": "Daily Standup",
        "start": (NOW - timedelta(seconds=20)).isoformat(),
        "end": (NOW + timedelta(minutes=15)).isoformat(),
    }
    return SimpleNamespace(events=AsyncMock(return_value={"events": [event]}))


def moments(tmp_path, sense=None):
    made = Moments(
        tmp_path / "db", calendar=calendar(), sense=sense or FakeMac(), clock=lambda: NOW
    )
    made.notify = AsyncMock()
    return made


async def test_an_app_coming_to_the_front(tmp_path):
    m = moments(tmp_path)
    once = await m.add("When I open Figma, remind me to check Ana's feedback")
    every = await m.add("whenever I open Slack remind me to update my status")
    assert once["once"] == 1 and every["once"] == 0 and once["when"] == "When you open Figma"
    assert await m.tick() == 0  # Mail is in front.
    m.sense.front = {"app": "Figma", "bundle_id": "com.figma.Desktop"}
    assert await m.tick() == 1
    title, body, view = m.notify.await_args.args
    assert title == "⏰ Check Ana's feedback" and body == "You opened Figma."
    assert await m.tick() == 0  # Still Figma: not again.
    for _ in range(2):
        m.sense.front = {"app": "Slack", "bundle_id": "com.tinyspeck.slackmacgap"}
        assert await m.tick() == 1
        m.sense.front = {"app": "Mail", "bundle_id": "com.apple.mail"}
        await m.tick()
    statuses = {x["text"]: x["status"] for x in m.overview()}
    assert statuses == {"Check Ana's feedback": "done", "Update my status": "active"}


async def test_back_at_the_mac_after_being_away(tmp_path):
    m = moments(tmp_path)
    await m.add("remind me to call the bank when I'm back at my desk")
    m.sense.idle = 60
    assert await m.tick() == 0  # A minute isn't "away".
    m.sense.idle = 1
    assert await m.tick() == 0
    m.sense.idle = AWAY_SECONDS + 10
    assert await m.tick() == 0
    m.sense.idle = 1
    assert await m.tick() == 1
    assert m.notify.await_args.args[:2] == ("⏰ Call the bank", "Welcome back.")


async def test_a_meeting_starting(tmp_path):
    m = moments(tmp_path)
    await m.add("remind me to ask about the budget when the standup starts")
    await m.add("remind me to fill in the retro when the retro starts")
    assert await m.tick() == 1
    assert m.notify.await_args.args[1] == "“Daily Standup” is starting."
    m._meetings_at = 0
    assert await m.tick() == 0  # Once per meeting.


async def test_unclear_words_and_no_mac(tmp_path):
    m = moments(tmp_path)
    with pytest.raises(ValueError, match="Say when"):
        await m.add("remind me about the deck")
    m.llm = SimpleNamespace(
        complete=AsyncMock(
            return_value='{"kind": "app", "target": "Xcode", "text": "Run the tests"}'
        )
    )
    made = await m.add("next time I'm in Xcode, run the tests")
    assert made["when"] == "When you open Xcode"
    (tmp_path / "other").mkdir()
    nowhere = moments(tmp_path / "other")
    nowhere.sense = None
    await nowhere.add("remind me to stretch when I'm back")
    assert await nowhere.tick() == 0


async def test_the_one_box_knows_these():
    assert await kind_of("remind me to ask about the budget when the standup starts") == "moment"
    assert await kind_of("When I open Figma, remind me to check the feedback") == "moment"
    assert await kind_of("tell me when PR 412 is merged") == "watch"


def test_moment_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.bootstrap import build_agent
    from app.config.settings import Settings

    monkeypatch.chdir(tmp_path)
    config = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    agent = build_agent(config, llm=AsyncMock())
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(config, agent, enable_ui=True)) as client:
        made = client.post(
            "/api/v1/moments", json={"text": "remind me to stretch when I'm back"}, headers=headers
        ).json()
        assert made["when"] == "When you're back at your Mac"
        listed = client.get("/api/v1/moments", headers=headers).json()
        assert listed["available"] is False and len(listed["moments"]) == 1
        assert (
            client.post(
                "/api/v1/moments", json={"text": "remind me about stuff"}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/moments/remove", json={"id": made["id"]}, headers=headers
            ).status_code
            == 200
        )
        assert client.get("/api/v1/moments").status_code == 401
    assert "moment_reminder" in [t.name for t in agent.executor.registry.tools()]
