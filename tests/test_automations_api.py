"""The Automations screen's routes."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from tests.test_dashboard_api import headers


@pytest.fixture
def auto(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="local-test-token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    with TestClient(
        create_app(settings, agent, enable_ui=True), base_url="http://127.0.0.1:8000"
    ) as client:
        yield client, agent


def test_routines_own_their_schedules(auto):
    client, agent = auto
    assert (
        client.post(
            "/api/v1/proactive/settings", json={"morning_plan": True}, headers=headers()
        ).status_code
        == 200
    )
    now = datetime.now().astimezone()
    mine = agent.schedule_store.add("Brief me on the week", "weekly", "09:00", 0, now)
    data = client.get("/api/v1/automations", headers=headers()).json()
    assert [s["message"] for s in data["schedules"]] == [
        "Brief me on the week"
    ]  # Not "Plan my day".
    assert data["schedules"][0]["when"] == "every Monday at 09:00"
    assert data["settings"]["morning_plan"] is True
    routine = data["settings"]["morning_schedule_id"]
    response = client.post("/api/v1/schedules/delete", json={"id": routine}, headers=headers())
    assert response.status_code == 409 and "switch" in response.json()["detail"]
    assert (
        client.post("/api/v1/schedules/delete", json={"id": mine.id}, headers=headers()).status_code
        == 200
    )
    assert (
        client.post("/api/v1/schedules/delete", json={"id": mine.id}, headers=headers()).status_code
        == 404
    )


def test_watches_and_followups_are_listed(auto):
    client, agent = auto
    agent.proactive_store.add_watch("email", "from:olivier", "Email: from:olivier")
    due = (datetime.now().astimezone() + timedelta(days=2)).isoformat()
    agent.proactive_store.add_followup("Sarah", "s@example.com", "contract", due)
    data = client.get("/api/v1/automations", headers=headers()).json()
    assert data["watches"][0]["query"] == "from:olivier"
    assert data["followups"][0]["name"] == "Sarah"
    assert client.get("/api/v1/automations").status_code == 401
    assert client.get("/ui/automations.js").status_code == 200
