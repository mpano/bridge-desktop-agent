"""First-launch setup routes. OpenAI and the Keychain are faked; no real key is used."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api import onboarding
from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm import keychain
from tests.test_dashboard_api import headers

FAKE_KEY = "sk-test" + "a" * 30


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=\nAPI_TOKEN=local-test-token\n")
    saved = {}
    monkeypatch.setattr(keychain, "save_key", lambda value: saved.update(key=value))
    monkeypatch.setattr(keychain, "loaded_from", "")
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="local-test-token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    with TestClient(
        create_app(settings, agent, enable_ui=True), base_url="http://127.0.0.1:8000"
    ) as client:
        yield client, agent, settings, saved, tmp_path / ".env"


def test_state_starts_unfinished_without_a_key(setup):
    client, *_ = setup
    data = client.get("/api/v1/onboarding", headers=headers()).json()
    assert data["done"] is False and data["openai"] == {"set": False, "source": ""}
    assert set(data["days"]) == {
        "work_start",
        "work_end",
        "morning_plan",
        "morning_time",
        "meeting_prep",
    }


def test_a_working_key_goes_to_the_keychain_not_a_file(setup, monkeypatch):
    client, _, settings, saved, env = setup
    monkeypatch.setattr(onboarding, "key_works", AsyncMock(return_value=True))
    response = client.post(
        "/api/v1/onboarding/openai-key", json={"key": FAKE_KEY}, headers=headers()
    )
    assert response.json() == {"saved": True, "source": "keychain"}
    assert saved["key"] == FAKE_KEY and settings.openai_api_key.get_secret_value() == FAKE_KEY
    assert FAKE_KEY not in env.read_text()
    assert (
        client.get("/api/v1/onboarding", headers=headers()).json()["openai"]["source"] == "keychain"
    )


def test_a_rejected_key_is_not_saved(setup, monkeypatch):
    client, _, settings, saved, _ = setup
    monkeypatch.setattr(onboarding, "key_works", AsyncMock(return_value=False))
    response = client.post(
        "/api/v1/onboarding/openai-key", json={"key": FAKE_KEY}, headers=headers()
    )
    assert response.status_code == 422 and not saved
    assert not settings.openai_api_key.get_secret_value()
    bad = client.post("/api/v1/onboarding/openai-key", json={"key": "not a key"}, headers=headers())
    assert bad.status_code == 422


def test_moving_an_env_key_blanks_it_in_the_file(setup):
    client, _, settings, saved, env = setup
    env.write_text(f"OPENAI_API_KEY={FAKE_KEY}\nAPI_TOKEN=local-test-token\n")
    from pydantic import SecretStr

    settings.openai_api_key = SecretStr(FAKE_KEY)
    assert client.post("/api/v1/onboarding/openai-key/move", headers=headers()).json()["moved"]
    assert saved["key"] == FAKE_KEY
    assert (
        "OPENAI_API_KEY=\n" in env.read_text() and "API_TOKEN=local-test-token" in env.read_text()
    )


def test_finishing_is_remembered_and_shortcut_use_is_seen(setup):
    client, agent, *_ = setup
    agent.planner.llm.complete = AsyncMock(return_value="Hello there, team.")
    client.post(
        "/api/v1/text/dictation",
        json={"text": "hello there team", "app": "Notes"},
        headers=headers(),
    )
    client.post(
        "/api/v1/tasks", json={"message": "hi"}, headers={**headers(), "X-Bridge-Source": "voice"}
    )
    usage = client.get("/api/v1/onboarding", headers=headers()).json()["usage"]
    assert "dictate" in usage and "talk" in usage and "act" not in usage
    assert client.post(
        "/api/v1/onboarding/done", json={"done": True}, headers=headers()
    ).json() == {"done": True}
    assert client.get("/api/v1/onboarding", headers=headers()).json()["done"] is True
    assert (
        client.post(
            "/api/v1/permissions/request", json={"key": "camera"}, headers=headers()
        ).status_code
        == 422
    )
    assert client.get("/ui/onboarding.js").status_code == 200
