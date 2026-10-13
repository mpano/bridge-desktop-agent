"""Activity and Undo. Every service is a fake; nothing is sent or deleted for real."""

import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import Field

from app.llm.models import ToolCall
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class Anything(Input):
    channel: str = Field(default="", max_length=80)
    text: str = Field(default="", max_length=500)
    key: str = Field(default="", max_length=30)
    status: str = Field(default="", max_length=60)
    to: list[str] = Field(default_factory=list)
    subject: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=200)
    start: str = Field(default="", max_length=40)
    end: str = Field(default="", max_length=40)


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    from app.bootstrap import build_agent
    from app.config.settings import Settings

    monkeypatch.chdir(tmp_path)
    agent = build_agent(
        Settings(_env_file=None, database_path=tmp_path / "db", api_token="token"), llm=AsyncMock()
    )
    results = {
        "slack_send_message": {
            "channel_id": "C1",
            "message_ts": "171.5",
            "destination": "#releases",
        },
        "jira_move": {"key": "NW-151", "status": "Done", "from_status": "In Review"},
        "email_send": {"to": ["ana@acme.co"], "message_id": "m1"},
        "mac_calendar_create_event": {"title": "Sync", "start": "2026-10-15T10:00:00+02:00"},
    }
    deleted = []

    def fake(name):
        async def run(args):
            if name == "mac_calendar_delete_event":
                deleted.append(args.model_dump())
                return {"ok": True}
            return dict(results[name])

        return run

    for name in [*results, "mac_calendar_delete_event"]:
        try:
            agent.executor.registry.get(name)
            agent.executor.registry.get(name).handler = fake(name)
            agent.executor.registry.get(name).input_schema = Anything
        except ValueError:
            agent.executor.registry.register(
                Tool(name, name, Anything, RiskLevel.CONFIRM, fake(name))
            )
    agent.accounts = SimpleNamespace(request=AsyncMock(return_value={"ok": True}))
    agent.jira = SimpleNamespace(
        move=AsyncMock(return_value={"key": "NW-151", "status": "In Review"})
    )
    return SimpleNamespace(agent=agent, deleted=deleted)


async def do(agent, name, **arguments):
    return await agent.executor.execute(
        ToolCall(call_id="c", name=name, arguments=arguments), "r1", approved=True
    )


async def test_actions_are_written_down_and_undone(bridge):
    agent = bridge.agent
    sent = await do(agent, "slack_send_message", channel="#releases", text="🚀 Faster sync shipped")
    moved = await do(agent, "jira_move", key="NW-151", status="Done")
    mailed = await do(agent, "email_send", to=["ana@acme.co"], subject="Deck")
    assert sent["undo_id"] and moved["undo_id"] and "undo_id" not in mailed
    listed = {a["tool"]: a for a in agent.actions.recent()}
    assert (
        listed["slack_send_message"]["summary"] == "Posted in #releases: “🚀 Faster sync shipped”"
    )
    assert listed["jira_move"]["summary"] == "Moved NW-151 from In Review to Done"
    assert listed["email_send"]["can_undo"] is False
    assert listed["email_send"]["why_not"] == "An email can't be unsent once it's delivered."

    undone = await agent.actions.undo(sent["undo_id"])
    assert undone["status"] == "undone" and undone["can_undo"] is False
    args = agent.accounts.request.await_args
    assert args.args[1:4] == ("slack", "POST", "chat.delete")
    assert args.kwargs["json"] == {"channel": "C1", "ts": "171.5"}
    with pytest.raises(ValueError, match="can't be undone"):
        await agent.actions.undo(sent["undo_id"])  # Not twice.

    await agent.actions.undo(moved["undo_id"])
    agent.jira.move.assert_awaited_once_with("NW-151", "In Review")


async def test_calendar_events_are_removed_with_bridges_own_tool(bridge):
    agent = bridge.agent
    made = await do(
        agent,
        "mac_calendar_create_event",
        title="Sync",
        start="2026-10-15T10:00:00+02:00",
        end="2026-10-15T10:30:00+02:00",
    )
    await agent.actions.undo(made["undo_id"])
    assert bridge.deleted[0]["title"] == "Sync" and bridge.deleted[0]["start"].startswith(
        "2026-10-15T10:00"
    )
    # The deletion done by Undo isn't itself listed as something to undo.
    assert [a["tool"] for a in agent.actions.recent()] == ["mac_calendar_create_event"]


async def test_old_or_failing_undos(bridge, tmp_path):
    agent = bridge.agent
    sent = await do(agent, "slack_send_message", channel="#releases", text="hi")
    agent.accounts.request.side_effect = RuntimeError("network")
    with pytest.raises(ValueError, match="Couldn't undo"):
        await agent.actions.undo(sent["undo_id"])
    assert agent.actions.get(sent["undo_id"])["status"] == "done"  # You can try again.
    with sqlite3.connect(tmp_path / "db") as db:
        db.execute("UPDATE bridge_actions SET at = ?", (time.time() - 2 * 86400,))
    old = agent.actions.view(agent.actions.get(sent["undo_id"]))
    assert old["can_undo"] is False and old["why_not"] == "Too long ago to undo from Bridge."


def test_activity_routes(bridge, tmp_path):
    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.config.settings import Settings
    from app.phone.access import phone_may_use

    agent = bridge.agent
    headers = {"Authorization": "Bearer token"}
    with TestClient(
        create_app(
            Settings(_env_file=None, database_path=tmp_path / "db", api_token="token"),
            agent,
            enable_ui=True,
        )
    ) as client:
        client.portal.call(lambda: do(agent, "slack_send_message", channel="#x", text="hello"))
        [item] = client.get("/api/v1/actions", headers=headers).json()["actions"]
        assert item["can_undo"] is True
        assert (
            client.post("/api/v1/actions/undo", json={"id": item["id"]}, headers=headers).json()[
                "status"
            ]
            == "undone"
        )
        again = client.post("/api/v1/actions/undo", json={"id": item["id"]}, headers=headers)
        assert again.status_code == 409
        assert "BridgeActivity" in client.get("/ui/activity.js").text
        assert client.get("/api/v1/actions").status_code == 401
    assert phone_may_use("POST", "/api/v1/actions/undo")
