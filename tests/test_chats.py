"""Recent chats in Ask: kept on this Mac for the chosen days, carried on after a restart."""

import sqlite3
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.assistant.chats import IDLE_SECONDS, ChatStore
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm.models import LLMResponse

HEADERS = {"Authorization": "Bearer token"}


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def entry(role, text):
    return {"role": role, "text": text, "at": "2026-10-04T09:00:00+02:00"}


def test_store_saves_lists_and_prunes_old_chats(tmp_path):
    clock = Clock()
    store = ChatStore(tmp_path / "db", days=7, clock=clock)
    store.save("a" * 32, [entry("user", "  Plan   my day "), entry("assistant", "Done")], [])
    clock.now += 8 * 86400
    store.save("b" * 32, [entry("user", "Brief me")], [{"role": "user", "content": "Brief me"}])
    assert [(c["title"], c["messages"]) for c in store.recent()] == [
        ("Brief me", 1),
        ("Plan my day", 2),
    ]
    assert store.prune() == 1
    assert [c["id"] for c in store.recent()] == ["b" * 32]
    assert store.load("b" * 32)["history"] == [{"role": "user", "content": "Brief me"}]


def test_store_keeps_nothing_when_off(tmp_path):
    store = ChatStore(tmp_path / "db", days=7)
    store.save("a" * 32, [entry("user", "hi")], [])
    store.set_days(0)
    assert store.recent() == []
    store.save("b" * 32, [entry("user", "hi")], [])
    assert store.recent() == []
    assert store.latest() is None


def test_latest_only_carries_on_a_recent_chat(tmp_path):
    clock = Clock()
    store = ChatStore(tmp_path / "db", clock=clock)
    store.save("a" * 32, [entry("user", "hi")], [])
    assert store.latest()["id"] == "a" * 32
    clock.now += IDLE_SECONDS + 1
    assert store.latest() is None


def make(tmp_path, *texts):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    llm = AsyncMock()
    llm.generate_response.side_effect = [LLMResponse(text=text) for text in texts]
    agent = build_agent(settings, llm=llm, runner=AsyncMock())
    agent.scheduler = None
    return settings, agent


def ask(client, message):
    request_id = client.post("/api/v1/tasks", json={"message": message}, headers=HEADERS).json()[
        "request_id"
    ]
    for _ in range(200):
        if client.get(f"/api/v1/tasks/{request_id}", headers=HEADERS).json()["result"]:
            return


def test_chat_survives_a_restart_and_new_keeps_the_old_one(tmp_path):
    settings, agent = make(tmp_path, "Hello!", "Sure.")
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        ask(client, "Hi Bridge")
        first = client.get("/api/v1/conversation", headers=HEADERS).json()["chat_id"]
        client.post("/api/v1/conversation/reset", headers=HEADERS)
        ask(client, "Plan my day")

    settings, restarted = make(tmp_path)
    with TestClient(create_app(settings, restarted, enable_ui=True)) as client:
        convo = client.get("/api/v1/conversation", headers=HEADERS).json()
        assert [m["text"] for m in convo["messages"]][0] == "Plan my day"
        # The model gets the chat's context back, not just the screen.
        assert restarted.history[0] == {"role": "user", "content": "Plan my day"}
        recent = client.get("/api/v1/chats", headers=HEADERS).json()
        assert recent["days"] == 7 and recent["current"] == convo["chat_id"]
        assert [c["title"] for c in recent["chats"]] == ["Plan my day", "Hi Bridge"]

        opened = client.post("/api/v1/chats/open", json={"id": first}, headers=HEADERS).json()
        assert opened["chat_id"] == first
        assert [m["text"] for m in opened["messages"]] == ["Hi Bridge", "Hello!"]
        missing = client.post("/api/v1/chats/open", json={"id": "f" * 32}, headers=HEADERS)
        assert missing.status_code == 404
        assert (
            client.post("/api/v1/chats/open", json={"id": "../x"}, headers=HEADERS).status_code
            == 422
        )

        # Deleting the open chat starts a fresh one.
        client.post("/api/v1/chats/delete", json={"id": first}, headers=HEADERS)
        assert client.get("/api/v1/conversation", headers=HEADERS).json()["messages"] == []
        bad = client.post("/api/v1/chats/delete", json={}, headers=HEADERS)
        assert bad.status_code == 422
        deleted = client.post("/api/v1/chats/delete", json={"all": True}, headers=HEADERS).json()
        assert deleted["deleted"] == 1
        assert client.get("/api/v1/chats", headers=HEADERS).json()["chats"] == []
        assert client.get("/api/v1/chats").status_code == 401


def test_back_after_a_break_starts_a_new_chat(tmp_path):
    settings, agent = make(tmp_path, "Hello!", "Sure.")
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        ask(client, "Hi Bridge")
        first = agent.chat_id
        agent.last_active -= IDLE_SECONDS + 1
        ask(client, "Plan my day")
        assert agent.chat_id != first
        assert [m["text"] for m in agent.conversation()][0] == "Plan my day"
        assert agent.history[0] == {"role": "user", "content": "Plan my day"}
        assert len(client.get("/api/v1/chats", headers=HEADERS).json()["chats"]) == 2


def test_only_what_you_saw_is_stored(tmp_path):
    settings, agent = make(tmp_path, "Hello!")
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        ask(client, "Hi Bridge")
    with sqlite3.connect(tmp_path / "db") as db:
        (transcript,) = db.execute("SELECT transcript FROM chats").fetchone()
    assert "token" not in transcript.lower()


def test_keep_chats_setting_applies_without_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings, agent = make(tmp_path, "Hello!")
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        ask(client, "Hi Bridge")
        read = client.get("/api/v1/settings/app", headers=HEADERS).json()
        assert read["privacy"]["keep_chats_days"] == 7
        saved = client.post(
            "/api/v1/settings/app", json={"keep_chats_days": 0}, headers=HEADERS
        ).json()
        assert saved == {"saved": ["CHAT_RETENTION_DAYS"], "restart_needed": False}
        assert "CHAT_RETENTION_DAYS=0" in (tmp_path / ".env").read_text()
        assert client.get("/api/v1/chats", headers=HEADERS).json() == {
            "chats": [],
            "current": agent.chat_id,
            "days": 0,
        }
        bad = client.post("/api/v1/settings/app", json={"keep_chats_days": 3}, headers=HEADERS)
        assert bad.status_code == 422
