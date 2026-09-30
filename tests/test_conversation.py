"""The dashboard's conversation log and step progress."""

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm.models import LLMResponse, ToolCall


def test_conversation_survives_page_reload_and_reset_clears_it(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    llm = AsyncMock()
    llm.generate_response.side_effect = [
        LLMResponse(calls=[ToolCall(call_id="1", name="list_projects", arguments={})]),
        LLMResponse(text="You have no projects."),
    ]
    agent = build_agent(settings, llm=llm, runner=AsyncMock())
    agent.scheduler = None
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        started = client.post("/api/v1/tasks", json={"message": "List projects"}, headers=headers)
        request_id = started.json()["request_id"]
        for _ in range(200):
            progress = client.get(f"/api/v1/tasks/{request_id}", headers=headers).json()
            if progress["result"]:
                break
        assert progress["steps"] == [
            {"tool": "list_projects", "success": True, "status": "completed"}
        ]
        messages = client.get("/api/v1/conversation", headers=headers).json()["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["text"] == "List projects"
        assert messages[1]["text"].startswith("You have no projects.")
        assert messages[1]["steps"][0]["tool"] == "list_projects"
        assert all(m["request_id"] == request_id for m in messages)
        assert client.get("/api/v1/conversation").status_code == 401
        client.post("/api/v1/conversation/reset", headers=headers)
        assert client.get("/api/v1/conversation", headers=headers).json()["messages"] == []
