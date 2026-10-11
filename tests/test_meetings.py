"""Meeting notes. A fake microphone, fake speech-to-text, a fake model and calendar: nothing
is ever recorded, transcribed for real, or sent."""

import asyncio
import json
import time
import wave
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from app.assistant.commitments import CommitmentStore
from app.assistant.meetings import MeetingError, Meetings

ZONE = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 13, 14, 2, tzinfo=ZONE)


class FakeEars:
    made: list = []

    def __init__(self):
        self.callback = None
        self.stopped = False
        FakeEars.made.append(self)

    def start(self, callback):
        self.callback = callback
        return ""

    def stop(self):
        self.stopped = True


class FakeSTT:
    def __init__(self):
        self.said: list[str] = []
        self.files: list[tuple[int, int]] = []

    async def transcribe(self, path):
        with wave.open(str(path)) as audio:
            self.files.append((audio.getframerate(), audio.getnchannels()))
        return self.said.pop(0) if self.said else ""


SUMMARY = {
    "title": "Design review",
    "summary": "We reviewed the new onboarding.",
    "decisions": ["Ship onboarding on Friday"],
    "actions": [
        {
            "owner": "you",
            "person": "you",
            "what": "Send the deck to Ana",
            "due": "2026-10-14",
            "quote": "I'll send the deck to Ana tomorrow",
        },
        {
            "owner": "them",
            "person": "Sam",
            "what": "Fix the login bug",
            "due": "Friday",
            "quote": "Sam: I'll fix the login bug",
        },
    ],
    "questions": ["Who owns analytics?"],
    "followup": {"subject": "Design review follow-up", "body": "Thanks all! …"},
}


def llm():
    async def complete(instructions, text):
        if "live notes" in instructions:
            return json.dumps({"notes": ["Onboarding ships Friday"]})
        if "write up a meeting" in instructions:
            return json.dumps(SUMMARY)
        return "Sam will fix the login bug."

    return SimpleNamespace(complete=AsyncMock(side_effect=complete))


def calendar(video=True, start=NOW - timedelta(minutes=2)):
    event = {
        "id": "ev1",
        "title": "Design review",
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=30)).isoformat(),
        "video": video,
        "attendees": [
            {"name": "Ana", "email": "ana@acme.co"},
            {"name": "Sam", "email": "sam@acme.co"},
        ],
    }
    return SimpleNamespace(events=AsyncMock(return_value={"events": [event]}))


def meetings(tmp_path, **kwargs):
    settings = SimpleNamespace(meetings_enabled=True, meeting_retention_days=30)
    made = Meetings(
        tmp_path / "db",
        settings,
        llm=kwargs.pop("llm", llm()),
        stt=kwargs.pop("stt", FakeSTT()),
        calendar=kwargs.pop("calendar", calendar()),
        commitments=SimpleNamespace(store=CommitmentStore(tmp_path / "db")),
        gmail=kwargs.pop("gmail", AsyncMock()),
        audio_factory=kwargs.pop("audio_factory", FakeEars),
        clock=lambda: NOW,
    )
    made.notify = AsyncMock()
    return made


SOUND = np.full(16000 * 2, 0.1, dtype=np.float32)


async def until(check, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def test_a_meeting_from_start_to_notes(tmp_path):
    m = meetings(tmp_path)
    started = await m.start()
    assert started["title"] == "Design review" and started["status"] == "recording"
    assert [p["name"] for p in started["attendees"]] == ["Ana", "Sam"]
    ears = FakeEars.made[-1]
    t0 = m.current["started"]
    m.stt.said = [
        "Let's ship onboarding on Friday.",
        "I'll send the deck to Ana tomorrow.",
        "Let's ship onboarding on Friday.",
        "Thank you.",
        "I'll fix the login bug.",
    ]
    ears.callback("others", t0 + 1, t0 + 4, SOUND)
    ears.callback("you", t0 + 5, t0 + 8, SOUND)
    ears.callback("you", t0 + 2, t0 + 4, SOUND)  # The mic heard the speakers: an echo.
    ears.callback("others", t0 + 9, t0 + 10, SOUND)  # Whisper "hearing" silence.
    ears.callback("others", t0 + 11, t0 + 14, SOUND)
    await until(lambda: m._queue.empty() and len(m.stt.said) == 0)
    await asyncio.sleep(0.05)
    lines = [(s["who"], s["text"]) for s in m.current["segments"]]
    assert lines == [
        ("Others", "Let's ship onboarding on Friday."),
        ("You", "I'll send the deck to Ana tomorrow."),
        ("Others", "I'll fix the login bug."),
    ]
    assert m.stt.files[0] == (16000, 1)  # Each piece goes to Whisper as 16 kHz mono.
    assert "Sam will fix" in await m.ask("Who fixes the login bug?")

    m.pause(True)
    ears.callback("you", t0 + 20, t0 + 22, SOUND)  # Paused: not heard.
    assert m._queue.empty()
    m.pause(False)

    stopping = await m.stop()
    assert stopping["status"] == "processing" and ears.stopped
    await until(lambda: m.current is None)
    done = m.store.get(started["id"])
    assert done["status"] == "done" and done["summary"]["decisions"] == [
        "Ship onboarding on Friday"
    ]
    actions = done["summary"]["actions"]
    assert actions[0]["due"] == "2026-10-14" and actions[1]["due"] is None  # "Friday" isn't a date.
    promises = m.commitments.store.items("open")
    mine = [p for p in promises if p["direction"] == "mine"]
    theirs = [p for p in promises if p["direction"] == "theirs"]
    assert mine[0]["what"] == "Send the deck to Ana" and mine[0]["source"] == "meeting"
    assert mine[0]["quote"] == "I'll send the deck to Ana tomorrow"
    assert theirs[0]["person"] == "Sam"
    title, body, view = m.notify.await_args.args
    assert title == "Notes ready: Design review" and "2 action items" in body and view == "meetings"
    transcript_sent = m.llm.complete.await_args.args[1]
    assert "Others: Let's ship onboarding" in transcript_sent and "Thank you" not in transcript_sent


async def test_the_follow_up_goes_exactly_as_shown(tmp_path):
    gmail = AsyncMock()
    m = meetings(tmp_path, gmail=gmail)
    started = await m.start()
    m.stt.said = ["Hello everyone."]
    FakeEars.made[-1].callback("others", time.time(), time.time() + 2, SOUND)
    await until(lambda: m.current["segments"])
    await m.stop()
    await until(lambda: m.current is None)
    sent = await m.send_followup(started["id"], ["ana@acme.co"], "Thanks", "See you Friday.")
    args = gmail.send.await_args.args[0]
    assert (
        args.to == ["ana@acme.co"] and args.subject == "Thanks" and args.body == "See you Friday."
    )
    assert sent["sent"][0]["to"] == ["ana@acme.co"]
    assert sent["summary"]["followup"] == {"subject": "Thanks", "body": "See you Friday."}


async def test_turned_on_and_one_at_a_time(tmp_path):
    m = meetings(tmp_path)
    m.settings.meetings_enabled = False
    with pytest.raises(MeetingError, match="Turn on Meeting notes"):
        await m.start()
    m.settings.meetings_enabled = True
    m.audio_factory = None
    with pytest.raises(MeetingError, match="Bridge app on your Mac"):
        await m.start()
    m.audio_factory = FakeEars
    started = await m.start("Standup")
    assert started["title"] == "Standup"
    with pytest.raises(MeetingError, match="already taking notes"):
        await m.start()
    with pytest.raises(MeetingError, match="Stop the meeting first"):
        m.delete(started["id"])
    await m.stop()
    await until(lambda: m.current is None)
    assert m.store.get(started["id"])["summary"]["summary"] == ""  # Nothing was said.
    assert "Nothing was said" in m.notify.await_args.args[1]
    assert m.delete(started["id"]) is True


async def test_offers_to_take_notes_when_a_video_meeting_starts(tmp_path):
    from app.workflows.watches import ProactiveStore

    m = meetings(tmp_path)
    m.proactive_store = ProactiveStore(tmp_path / "db")
    await m.check()
    assert m.notify.await_args.args[0] == "Take notes for “Design review”?"
    assert m.suggestion["title"] == "Design review"
    await m.check()
    assert m.notify.await_count == 1  # Once per meeting.

    (tmp_path / "other").mkdir()
    quiet = meetings(tmp_path / "other", calendar=calendar(video=False))
    quiet.proactive_store = m.proactive_store
    await quiet.check()
    quiet.notify.assert_not_awaited()
    assert quiet.suggestion is None


async def test_old_meetings_are_deleted(tmp_path):
    m = meetings(tmp_path)
    started = await m.start()
    await m.stop()
    await until(lambda: m.current is None)
    record = m.store.get(started["id"])
    record["started"] = time.time() - 40 * 86400
    m.store.save(record)
    assert m.overview()["meetings"] == []


def test_meeting_routes_and_phone(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.bootstrap import build_agent
    from app.config.settings import Settings
    from app.phone.access import MAC_ONLY_TOOLS, phone_may_use

    monkeypatch.chdir(tmp_path)
    config = Settings(
        _env_file=None, database_path=tmp_path / "db", api_token="token", openai_api_key="sk-x"
    )
    agent = build_agent(config, llm=AsyncMock())
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(config, agent, enable_ui=True)) as client:
        overview = client.get("/api/v1/meetings", headers=headers).json()
        assert overview["enabled"] is False and overview["available"] is False
        refused = client.post("/api/v1/meetings/start", json={}, headers=headers)
        assert refused.status_code == 409 and "Turn on Meeting notes" in refused.json()["detail"]
        saved = client.post(
            "/api/v1/settings/app",
            json={"meetings_enabled": True, "meeting_keep_days": 7},
            headers=headers,
        )
        assert saved.json()["restart_needed"] is False and config.meeting_retention_days == 7
        assert client.get("/api/v1/meetings", headers=headers).json()["keep_days"] == 7
        assert client.get("/api/v1/meetings/detail?id=abcdef12", headers=headers).status_code == 404
        bad = client.post(
            "/api/v1/meetings/followup",
            json={"id": "abcdef12", "to": ["sam"], "subject": "x", "body": "y"},
            headers=headers,
        )
        assert bad.status_code == 422
        assert "BridgeMeetings" in client.get("/ui/meetings.js").text
        assert "meeting-panel.js" in client.get("/ui/meeting.html").text
        assert client.get("/api/v1/meetings").status_code == 401
    assert phone_may_use("GET", "/api/v1/meetings/detail")
    assert not phone_may_use("POST", "/api/v1/meetings/start")
    assert "meeting_notes" in MAC_ONLY_TOOLS
    names = [t.name for t in agent.executor.registry.tools()]
    assert "meeting_notes" in names and "meeting_recall" in names
