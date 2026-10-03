"""The Today screen's routes, with the calendar, planner, inbox and focus faked."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm.models import ToolCall
from tests.test_dashboard_api import headers


@pytest.fixture
def today(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="local-test-token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    agent.calendar = AsyncMock()
    agent.calendar.events.return_value = {
        "events": [
            {
                "title": "Standup",
                "start": "2026-10-02T10:30:00+02:00",
                "end": "2026-10-02T10:45:00+02:00",
                "all_day": False,
                "busy": True,
                "notes": "x",
            }
        ]
    }
    agent.day_planner = AsyncMock()
    agent.inbox = None
    with TestClient(
        create_app(settings, agent, enable_ui=True), base_url="http://127.0.0.1:8000"
    ) as client:
        yield client, agent


def test_today_gathers_the_day_without_extra_fields(today):
    client, agent = today
    agent.proactive_store.add_followup(
        "Olivier", "o@example.com", "contract", "2030-01-03T17:00:00+00:00"
    )
    data = client.get("/api/v1/today", headers=headers()).json()
    assert data["events"] == [
        {
            "title": "Standup",
            "start": "2026-10-02T10:30:00+02:00",
            "end": "2026-10-02T10:45:00+02:00",
            "all_day": False,
            "location": None,
        }
    ]
    assert data["followups"][0]["name"] == "Olivier"
    assert data["plan"] is None and data["approvals"] == 0 and data["gmail"] is False
    assert data["focus"] == {"active": False}
    assert client.get("/api/v1/today").status_code == 401  # Signed-in owner only.


def test_calendar_errors_become_a_plain_hint(today):
    client, agent = today
    agent.calendar.events.side_effect = RuntimeError("denied")
    data = client.get("/api/v1/today", headers=headers()).json()
    assert data["events"] == [] and "Calendar access" in data["calendar_error"]


def test_plan_and_apply_use_the_stored_blocks(today):
    client, agent = today
    agent.day_planner.plan.return_value = {"plan_id": 1, "blocks": []}
    assert (
        client.post("/api/v1/today/plan", json={"day": "today"}, headers=headers()).status_code
        == 200
    )
    agent.day_planner.plan.side_effect = ValueError(
        "Your working day is over. Ask me to plan tomorrow instead."
    )
    response = client.post("/api/v1/today/plan", json={}, headers=headers())
    assert response.status_code == 422 and "tomorrow" in response.json()["detail"]
    blocks = [
        {
            "start": "2026-10-02T09:00:00+02:00",
            "end": "2026-10-02T10:00:00+02:00",
            "title": "Focus",
            "kind": "focus",
            "why": "",
        }
    ]
    plan_id = agent.proactive_store.save_plan("2026-10-02", blocks)
    agent.day_planner.apply.return_value = {"created": ["Focus"], "count": 1}
    assert (
        client.post(
            "/api/v1/today/plan/apply", json={"plan_id": plan_id}, headers=headers()
        ).json()["count"]
        == 1
    )
    agent.day_planner.apply.assert_awaited_once_with(blocks)
    assert (
        client.post(
            "/api/v1/today/plan/apply", json={"plan_id": 999}, headers=headers()
        ).status_code
        == 404
    )


def test_inbox_summary_comes_from_the_last_triage(today):
    client, agent = today
    assert client.post("/api/v1/inbox/triage", headers=headers()).status_code == 409
    groups = {
        "urgent": [
            {
                "from": "Olivier Mupenzi <o@example.com>",
                "subject": "Contract",
                "summary": "Sign today",
            }
        ],
        "reply": [],
        "fyi": [{}, {}],
        "newsletter": [],
    }
    agent.inbox = SimpleNamespace(last=(time.time(), {"groups": groups}), triage=AsyncMock())
    summary = client.get("/api/v1/inbox/summary", headers=headers()).json()["summary"]
    assert summary["fresh"] and summary["counts"] == {
        "urgent": 1,
        "reply": 0,
        "fyi": 2,
        "newsletter": 0,
    }
    assert summary["top"] == [
        {
            "group": "urgent",
            "source": "gmail",
            "from": "Olivier Mupenzi",
            "subject": "Contract",
            "summary": "Sign today",
        }
    ]


def test_pending_approvals_are_listed_with_the_exact_arguments(today):
    client, agent = today
    context = SimpleNamespace(request_id="r1", confirmation_message=None)
    call = ToolCall(
        call_id="c",
        name="email_send",
        arguments={"to": ["sarah@example.com"], "subject": "Hi", "body": "Hello"},
    )
    token = agent.confirmations.create(context, call)
    items = client.get("/api/v1/approvals", headers=headers()).json()["approvals"]
    assert items[0]["token"] == token and items[0]["arguments"]["to"] == ["sarah@example.com"]
    assert items[0]["action"] == "email_send" and items[0]["expires_in_seconds"] > 0
    assert client.get("/api/v1/today", headers=headers()).json()["approvals"] == 1


def test_focus_starts_from_the_button(today):
    client, agent = today
    agent.focus = Mock(start=AsyncMock(return_value={"until": "15:30", "skipped": [], "done": []}))
    response = client.post(
        "/api/v1/focus/start", json={"minutes": 90, "task": "docs"}, headers=headers()
    )
    assert response.json()["until"] == "15:30"
    agent.focus.start.assert_awaited_once_with(90, "docs")
    assert (
        client.post("/api/v1/focus/start", json={"minutes": 5}, headers=headers()).status_code
        == 422
    )


def test_today_screen_is_served(today):
    client, _ = today
    page = client.get("/").text
    assert 'id="view-today"' in page and "/ui/today.js" in page
    assert client.get("/ui/today.js").status_code == 200


def test_screen_peek_is_quiet_when_unavailable(today):
    client, agent = today
    agent.screen = None
    assert client.get("/api/v1/screen/peek", headers=headers()).json() == {"window": None}
    agent.screen = Mock(
        peek=AsyncMock(return_value={"app": "Slack", "window": "general", "private": False})
    )
    assert client.get("/api/v1/screen/peek", headers=headers()).json()["window"]["app"] == "Slack"
    agent.screen = Mock(peek=AsyncMock(side_effect=RuntimeError("AX")))
    assert client.get("/api/v1/screen/peek", headers=headers()).json() == {"window": None}
