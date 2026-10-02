"""The Settings screen's routes: they write only listed keys to .env, never secrets."""

import json
import stat
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.env_file import update_env
from app.config.settings import Settings
from tests.test_dashboard_api import headers

ENV = """# Bridge settings
OPENAI_API_KEY=sk-secret-value
REMOTE_TOOL_RESULTS=allowlist
REMOTE_TOOL_RESULT_ALLOWLIST=["email_search","screen_context"]
# a comment stays
DICTATION_CLEANUP=true
"""


@pytest.fixture
def app_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(ENV)
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db",
        api_token="local-test-token",
        remote_tool_results="allowlist",
        remote_tool_result_allowlist=["email_search", "screen_context"],
    )
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    with TestClient(
        create_app(settings, agent, enable_ui=True), base_url="http://127.0.0.1:8000"
    ) as client:
        yield client, tmp_path / ".env"


def test_env_writer_keeps_every_other_line(tmp_path):
    path = tmp_path / ".env"
    path.write_text(ENV)
    update_env(path, {"DICTATION_CLEANUP": "false", "VOICE_STT_PROVIDER": "openai"})
    text = path.read_text()
    assert "OPENAI_API_KEY=sk-secret-value" in text and "# a comment stays" in text
    assert "DICTATION_CLEANUP=false" in text and "DICTATION_CLEANUP=true" not in text
    assert text.rstrip().endswith("VOICE_STT_PROVIDER=openai")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_reading_settings_never_shows_secrets(app_settings):
    client, _ = app_settings
    data = client.get("/api/v1/settings/app", headers=headers()).json()
    assert data["privacy"]["share_results"] and data["privacy"]["share_screen"]
    assert data["privacy"]["openai_key"] is False  # Only whether one is set.
    assert "sk-secret" not in json.dumps(data)
    assert data["restart_needed"] is False and data["can_restart"] is False
    assert client.get("/api/v1/settings/app").status_code == 401


def test_privacy_switches_rewrite_the_allowlist(app_settings):
    client, env = app_settings
    response = client.post(
        "/api/v1/settings/app", json={"share_screen": False}, headers=headers()
    ).json()
    assert response["restart_needed"]
    assert 'REMOTE_TOOL_RESULT_ALLOWLIST=["email_search"]' in env.read_text()
    client.post("/api/v1/settings/app", json={"share_results": False}, headers=headers())
    text = env.read_text()
    assert "REMOTE_TOOL_RESULTS=status_only" in text and "REMOTE_TOOL_RESULT_ALLOWLIST=[]" in text
    client.post("/api/v1/settings/app", json={"share_results": True}, headers=headers())
    assert "REMOTE_TOOL_RESULTS=allowlist" in env.read_text() and "slack_search" in env.read_text()
    assert client.get("/api/v1/settings/app", headers=headers()).json()["restart_needed"]


def test_only_listed_settings_can_be_written(app_settings):
    client, env = app_settings
    bad = client.post("/api/v1/settings/app", json={"openai_api_key": "sk-x"}, headers=headers())
    assert bad.status_code == 422
    assert (
        client.post(
            "/api/v1/settings/app", json={"blocked_apps": ['Bad"App']}, headers=headers()
        ).status_code
        == 422
    )
    client.post(
        "/api/v1/settings/app",
        json={"blocked_apps": ["Banking", "Banking", " "], "speech": "openai"},
        headers=headers(),
    )
    text = env.read_text()
    assert 'SCREEN_CONTEXT_BLOCKED_APPS=["Banking"]' in text and "VOICE_STT_PROVIDER=openai" in text
    assert "OPENAI_API_KEY=sk-secret-value" in text


def test_restart_needs_the_app_bundle(app_settings, monkeypatch):
    client, _ = app_settings
    monkeypatch.delenv("BRIDGE_APP_BUNDLE", raising=False)
    assert client.post("/api/v1/app/restart", headers=headers()).status_code == 409


def test_new_screens_are_served(app_settings):
    client, _ = app_settings
    page = client.get("/").text
    assert 'id="view-settings"' in page and 'id="view-memory"' in page and "nav-more" not in page
    for element in (
        "preferences-form",
        "profile-form",
        "capability-list",
        "project-form",
        "memory-form",
    ):
        assert f'id="{element}"' in page  # Old features live on inside the new screens.
    assert client.get("/ui/settings.js").status_code == 200


async def test_notifications_go_through_the_native_poster(monkeypatch):
    from app.tools.system import notifications

    posted = []
    monkeypatch.setattr(
        notifications, "native", lambda title, message, view: posted.append((title, message, view))
    )
    runner = AsyncMock()
    await notifications.post_notification(runner, "Focus done ✓", "15 min.\n Nothing new.", "today")
    assert posted == [("Focus done ✓", "15 min. Nothing new.", "today")]
    runner.run.assert_not_awaited()  # No AppleScript when Bridge can post itself.

    def broken(*args):
        raise RuntimeError("not allowed")

    monkeypatch.setattr(notifications, "native", broken)
    await notifications.post_notification(runner, "Bridge", "Hi")
    runner.run.assert_awaited_once()  # Falls back to the script notification.


def test_test_notification_button(app_settings):
    client, _ = app_settings
    agent = client.app.state.agent
    agent.scheduler.notify = AsyncMock()
    assert client.post("/api/v1/notifications/test", headers=headers()).json() == {"sent": True}
    assert agent.scheduler.notify.await_args.kwargs == {"view": "today"}
