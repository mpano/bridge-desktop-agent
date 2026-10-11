"""Talking with Bridge: the realtime session, tools with approvals, privacy, the phone."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import Field

from app.api.server import create_app
from app.assistant.talk import Talk, TalkError
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class Words(Input):
    text: str = Field(default="hi", max_length=50)


def agent_with_tools(tmp_path, **settings):
    config = Settings(
        _env_file=None,
        database_path=tmp_path / "db",
        api_token="token",
        openai_api_key="sk-test-key",
        **settings,
    )
    agent = build_agent(config, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = agent.proactive = None
    sent = []

    async def look(args):
        return {"found": f"secret note: {args.text}"}

    async def send(args):
        sent.append(args.text)
        return {"sent": args.text}

    registry = agent.executor.registry
    registry.register(
        Tool(
            "look_up",
            "Look something up.",
            Words,
            RiskLevel.SAFE,
            look,
            render=lambda d: d["found"],
        )
    )
    registry.register(
        Tool(
            "send_note",
            "Send a note.",
            Words,
            RiskLevel.CONFIRM,
            send,
            confirmation_message="Send this note?",
        )
    )
    return agent, config, sent


class OpenAI:
    def __init__(self):
        self.seen = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(200, json={"value": "ek_short_lived", "expires_at": 123})


def test_a_conversation_starts_only_when_turned_on(tmp_path):
    agent, config, _ = agent_with_tools(tmp_path)
    openai = OpenAI()
    talk = Talk(
        tmp_path / "db",
        agent,
        config,
        http=httpx.AsyncClient(transport=httpx.MockTransport(openai)),
    )
    with pytest.raises(TalkError, match="Conversation mode"):
        asyncio.run(talk.start(from_phone=False))
    config.talk_enabled = True
    made = asyncio.run(talk.start(from_phone=False, name="Mpano"))
    assert made["client_secret"] == "ek_short_lived" and made["model"] == "gpt-realtime-2.1"
    request = openai.seen[0]
    assert request.headers["Authorization"] == "Bearer sk-test-key"
    assert "sk-test-key" not in json.dumps(made)  # Only the short-lived key reaches the page.
    session = json.loads(request.content)["session"]
    assert session["audio"]["input"]["turn_detection"]["interrupt_response"] is True
    assert session["audio"]["output"]["voice"] == "marin"
    names = {tool["name"] for tool in session["tools"]}
    assert {"look_up", "approve_pending", "end_conversation", "screen_context"} <= names
    assert all(
        set(tool) == {"type", "name", "description", "parameters"} for tool in session["tools"]
    )
    assert "Mpano" in session["instructions"] and "approve_pending" in session["instructions"]
    # From the phone: nothing that reads the Mac's screen, clipboard or Terminal.
    phone = asyncio.run(talk.start(from_phone=True))
    assert phone["session_id"] != made["session_id"]
    phone_tools = {t["name"] for t in json.loads(openai.seen[-1].content)["session"]["tools"]}
    assert "screen_context" not in phone_tools and "run_terminal_command" not in phone_tools


def test_tools_follow_privacy_and_wait_for_a_spoken_yes(tmp_path):
    agent, config, sent = agent_with_tools(tmp_path, talk_enabled=True)
    talk = Talk(
        tmp_path / "db",
        agent,
        config,
        http=httpx.AsyncClient(transport=httpx.MockTransport(OpenAI())),
    )
    session = asyncio.run(talk.start(from_phone=False))["session_id"]

    async def scenario():
        looked = await talk.tool(session, "look_up", {"text": "pricing"})
        asked = await talk.tool(session, "send_note", {"text": "on my way"})
        before_yes = list(sent)
        blocked = await talk.tool(session, "look_up", {"text": "again"})
        yes = await talk.tool(session, "approve_pending", {"approve": True})
        asked_again = await talk.tool(session, "send_note", {"text": "never mind"})
        no = await talk.approve(session, False)
        return looked, asked, before_yes, blocked, yes, asked_again, no

    looked, asked, before_yes, blocked, yes, asked_again, no = asyncio.run(scenario())
    # look_up isn't shared with the model: it hears "on screen", the screen shows the result.
    assert looked["for_model"] == {"success": True, "shown_on_screen": True}
    assert looked["display"] == "secret note: pricing"
    assert asked["for_model"]["needs_approval"] is True and asked["approval"]
    assert before_yes == []  # Nothing is sent before a yes.
    assert "waiting for the user's yes or no" in blocked["for_model"]["error"]
    assert yes["for_model"]["success"] is True and sent == ["on my way"]
    assert asked_again["approval"] and no["for_model"] == {"success": True, "cancelled": True}
    assert sent == ["on my way"]


def test_shared_results_reach_the_model(tmp_path):
    agent, config, _ = agent_with_tools(
        tmp_path,
        talk_enabled=True,
        remote_tool_results="allowlist",
        remote_tool_result_allowlist=["look_up"],
    )
    talk = Talk(
        tmp_path / "db",
        agent,
        config,
        http=httpx.AsyncClient(transport=httpx.MockTransport(OpenAI())),
    )
    session = asyncio.run(talk.start(from_phone=False))["session_id"]
    result = asyncio.run(talk.tool(session, "look_up", {"text": "x"}))
    assert result["for_model"]["result"]["result"]["found"] == "secret note: x"


def test_phone_conversations_cant_use_the_mac_screen(tmp_path):
    agent, config, _ = agent_with_tools(tmp_path, talk_enabled=True)
    talk = Talk(
        tmp_path / "db",
        agent,
        config,
        http=httpx.AsyncClient(transport=httpx.MockTransport(OpenAI())),
    )
    session = asyncio.run(talk.start(from_phone=True))["session_id"]
    result = asyncio.run(talk.tool(session, "screen_context", {}))
    assert result["for_model"] == {"success": False, "error": "That only works at the Mac."}


def test_what_was_said_is_kept_and_minutes_counted(tmp_path):
    agent, config, _ = agent_with_tools(tmp_path, talk_enabled=True)
    talk = Talk(
        tmp_path / "db",
        agent,
        config,
        http=httpx.AsyncClient(transport=httpx.MockTransport(OpenAI())),
    )
    session = asyncio.run(talk.start(from_phone=False))["session_id"]
    talk.sessions[session]["started"] -= 200  # A few minutes into the conversation.
    asyncio.run(talk.log(session, "user", "  What's   on today? "))
    asyncio.run(talk.log(session, "assistant", "Two meetings."))
    said = agent.conversation()
    assert [(m["role"], m["text"], m.get("spoken")) for m in said] == [
        ("user", "What's on today?", True),
        ("assistant", "Two meetings.", True),
    ]
    assert agent.history[-1] == {"role": "assistant", "content": "Two meetings."}
    assert talk.end(session, 90)["minutes"] == 1.5
    assert talk.end(session, 90)["minutes"] == 1.5  # Counted once.
    with pytest.raises(TalkError, match="ended"):
        asyncio.run(talk.tool(session, "look_up", {}))


def test_routes_and_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent, config, _ = agent_with_tools(tmp_path)
    agent.talk.http = httpx.AsyncClient(transport=httpx.MockTransport(OpenAI()))
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(config, agent, enable_ui=True)) as client:
        assert client.get("/api/v1/talk", headers=headers).json()["enabled"] is False
        assert client.post("/api/v1/talk/start", json={}, headers=headers).status_code == 409
        saved = client.post(
            "/api/v1/settings/app",
            json={"talk_enabled": True, "talk_voice": "cedar"},
            headers=headers,
        ).json()
        assert saved["restart_needed"] is False and "TALK_ENABLED" in saved["saved"]
        assert "TALK_ENABLED=true" in (tmp_path / ".env").read_text()
        made = client.post("/api/v1/talk/start", json={}, headers=headers).json()
        assert made["client_secret"] == "ek_short_lived"
        bad = client.post(
            "/api/v1/talk/tool",
            json={"session_id": made["session_id"], "name": "../x"},
            headers=headers,
        )
        assert bad.status_code == 422
        ok = client.post(
            "/api/v1/talk/tool",
            json={"session_id": made["session_id"], "name": "look_up", "arguments": {"text": "a"}},
            headers=headers,
        )
        assert ok.json()["display"] == "secret note: a"
        agent.talk.sessions[made["session_id"]]["started"] -= 60
        assert (
            client.post(
                "/api/v1/talk/said",
                json={"session_id": made["session_id"], "role": "user", "text": "hello"},
                headers=headers,
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/talk/end",
                json={"session_id": made["session_id"], "seconds": 30},
                headers=headers,
            ).json()["usage"]["minutes"]
            == 0.5
        )
        assert (
            client.post(
                "/api/v1/settings/app", json={"talk_voice": "robot"}, headers=headers
            ).status_code
            == 422
        )
        policy = client.get("/").headers["content-security-policy"]
        assert "connect-src 'self' https://api.openai.com" in policy
