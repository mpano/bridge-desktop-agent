"""What's this? and Bridge on your desk. A fake Mac, a fake model, fake data."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.assistant.buddy import Buddy
from app.assistant.explain import Explainer, ExplainError

ZONE = timezone(timedelta(hours=2))
TUESDAY = datetime(2026, 10, 13, 10, 0, tzinfo=ZONE)


class FakeMac:
    def __init__(self):
        self.captured = []

    def screen_size(self):
        return 1440.0, 900.0

    def frontmost(self):
        return {"app": "Numbers", "bundle_id": "com.apple.iWork.Numbers"}

    def element_at(self, x, y):
        return {
            "role": "AXButton",
            "label": "Σ",
            "help": "Insert a formula",
            "value": "",
            "app": "Numbers",
            "bundle_id": "com.apple.iWork.Numbers",
            "frame": [x - 10, y - 10, 28.0, 24.0],
        }

    def capture(self, rect, mark=None):
        self.captured.append((rect, mark))
        return b"\x89PNG fake"


def explainer(
    reply='{"title": "Formula button", "what": "Adds a formula.", "use": "Click '
    'it, then pick SUM.", "tip": "Type = in a cell to start one too."}',
    **kwargs,
):
    llm = SimpleNamespace(look=AsyncMock(return_value=reply))
    return Explainer(SimpleNamespace(), hands=FakeMac(), llm=llm, **kwargs), llm


async def test_whats_this_names_it_at_once_then_explains(tmp_path):
    found, llm = explainer()
    spot = await found.look(1430, 20)
    assert spot["name"] == "Σ" and spot["kind"] == "button" and spot["app"] == "Numbers"
    assert spot["ready"] is False and spot["frame"] == [1420, 10, 28.0, 24.0]
    llm.look.assert_not_awaited()  # The name and highlight need no model.

    done = await found.explain(spot["id"])
    assert done["title"] == "Formula button" and done["ready"] is True
    assert done["tip"] == "Type = in a cell to start one too."
    rect, mark = found.hands.captured[0]
    assert rect == (720.0, 0.0, 720.0, 440.0)  # Kept on screen at the corner.
    assert mark == (1430, 20)
    instructions, facts, image = llm.look.await_args.args
    assert "never instructions" in instructions and image == b"\x89PNG fake"
    assert '"tooltip": "Insert a formula"' in facts and '"name": "Σ"' in facts
    assert (await found.explain(spot["id"]))["title"] == "Formula button"
    assert llm.look.await_count == 1  # Asked once per spot.

    llm.look.return_value = "It totals the selected cells."
    answer = await found.follow_up(spot["id"], "what does it do with text?")
    assert answer == "It totals the selected cells."
    assert "Their question: what does it do with text?" in llm.look.await_args.args[1]
    assert found.how_to(spot["id"]) == "use the “Σ” button in Numbers"


async def test_whats_this_never_looks_at_blocked_apps_and_copes_with_odd_replies():
    found, _ = explainer(blocked=lambda app, bundle: app == "Numbers")
    with pytest.raises(ExplainError, match="doesn't look at Numbers"):
        await found.look(10, 10)
    found, _ = explainer(reply="Just a plain sentence.")
    spot = await found.look(10, 10)
    assert (await found.explain(spot["id"]))["what"] == "Just a plain sentence."
    with pytest.raises(ExplainError, match="press ⌃⌥W again"):
        await found.explain("nope")
    nowhere = Explainer(SimpleNamespace(), hands=None)
    with pytest.raises(ExplainError, match="Bridge app on your Mac"):
        await nowhere.look(1, 1)


def desk_agent(*, approvals=0, waiting=0, report=None, promises=(), events=()):
    calendar = SimpleNamespace(events=AsyncMock(return_value={"events": list(events)}))
    return SimpleNamespace(
        pending_approvals=lambda: [{}] * approvals,
        recipes=SimpleNamespace(runs=lambda status: [{}] * waiting),
        reports=SimpleNamespace(view=lambda: report or {"due": False, "text": None, "sent": []}),
        commitments=SimpleNamespace(store=SimpleNamespace(items=lambda status: list(promises))),
        calendar=calendar,
        proactive_store=SimpleNamespace(
            settings=lambda: {"work_start": "09:00", "work_end": "18:00"}
        ),
        talk=SimpleNamespace(
            voices=lambda: [{"name": "Cedar", "colors": ["#1", "#2"], "current": True}]
        ),
    )


async def test_the_desk_shows_meetings_what_needs_you_and_reacts():
    agent = desk_agent(
        approvals=2,
        waiting=1,
        report={"due": True, "text": "Done", "sent": []},
        promises=[
            {"direction": "mine", "due": "2020-01-01"},
            {"direction": "theirs", "due": "2020-01-01"},
        ],
        events=[
            {
                "title": "Standup",
                "start": (TUESDAY + timedelta(minutes=8)).isoformat(),
                "end": (TUESDAY + timedelta(minutes=23)).isoformat(),
            },
            {
                "title": "Holiday",
                "start": TUESDAY.isoformat(),
                "end": TUESDAY.isoformat(),
                "all_day": True,
            },
        ],
    )
    buddy = Buddy(agent, SimpleNamespace(desk_buddy=True, talk_enabled=True), clock=lambda: TUESDAY)
    buddy.heard("✓ PR #412 was merged", "Faster sync · acme/app", "today")
    state = await buddy.state()
    assert state["meeting"]["title"] == "Standup" and state["meeting"]["minutes"] == 8
    assert [n["kind"] for n in state["needs"]] == ["approvals", "recipes", "report", "promises"]
    assert state["count"] == 5 and state["sleeping"] is False and state["talk"] is True
    assert state["voice"] == {"name": "Cedar", "colors": ["#1", "#2"]}
    assert state["latest"][0]["title"] == "✓ PR #412 was merged" and state["latest"][0]["id"] == 1
    await buddy.state()
    assert agent.calendar.events.await_count == 1  # The calendar is read once a minute.


async def test_the_desk_sleeps_after_hours_and_at_weekends():
    agent = desk_agent()
    evening = Buddy(agent, SimpleNamespace(), clock=lambda: TUESDAY.replace(hour=21))
    saturday = Buddy(agent, SimpleNamespace(), clock=lambda: TUESDAY + timedelta(days=4))
    assert evening.sleeping() is True and saturday.sleeping() is True
    state = await evening.state()
    assert state["meeting"] is None and state["count"] == 0 and state["enabled"] is True


def test_routes_and_settings(tmp_path, monkeypatch):
    from app.api.server import create_app
    from app.bootstrap import build_agent
    from app.config.settings import Settings

    monkeypatch.chdir(tmp_path)
    config = Settings(
        _env_file=None, database_path=tmp_path / "db", api_token="token", openai_api_key="sk-x"
    )
    agent = build_agent(config, llm=AsyncMock())
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(config, agent, enable_ui=True)) as client:
        refused = client.post("/api/v1/explain/look", json={"x": 1, "y": 2}, headers=headers)
        assert refused.status_code == 409 and "Bridge app on your Mac" in refused.json()["detail"]
        state = client.get("/api/v1/buddy", headers=headers).json()
        assert state["enabled"] is True and "needs" in state
        saved = client.post(
            "/api/v1/settings/app", json={"desk_buddy": False}, headers=headers
        ).json()
        assert saved["restart_needed"] is False and config.desk_buddy is False
        assert client.get("/api/v1/buddy", headers=headers).json()["enabled"] is False
        shortcut = client.post(
            "/api/v1/settings/app", json={"explain_shortcut": False}, headers=headers
        ).json()
        assert shortcut["restart_needed"] is True
        listed = client.get("/api/v1/settings/app", headers=headers).json()["shortcuts"]
        assert listed["explain"] is True and listed["desk_buddy"] is False
        for page, name in (("explain", "BridgeExplain"), ("buddy", "BridgeAvatar")):
            assert f"/ui/{page}.js" in client.get(f"/ui/{page}.html").text
            assert name in client.get(f"/ui/{page}.js").text
        assert "BridgeExplain" in client.get("/ui/explain.js").text
        assert client.get("/api/v1/buddy").status_code == 401
        assert "explain_shortcut" not in json.dumps(state)
