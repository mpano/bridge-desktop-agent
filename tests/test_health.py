"""Settings › Health: each part of Bridge, whether it works, and its fix."""

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings


def test_health_lists_every_part_with_a_fix(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = agent.proactive = None
    agent.operator.hands = None
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        data = client.get("/api/v1/health", headers=headers).json()
        checks = {c["id"]: c for c in data["checks"]}
        # No key: broken, with the way to fix it (and no request left the Mac to find out).
        assert checks["openai"]["state"] == "error"
        assert checks["openai"]["fix"] == {
            "label": "Add a key",
            "view": "settings",
            "section": "privacy",
        }
        assert data["errors"] >= 1
        for key in ("gmail", "slack", "jira", "github", "phone", "talk", "operator"):
            assert checks[key]["state"] == "off" and checks[key]["fix"], key
        assert checks["operator"]["fix"]["section"] == "operator"
        assert checks["chats"]["state"] == "ok"
        assert {c["area"] for c in data["checks"]} >= {
            "Basics",
            "Connections",
            "Assistant",
            "Phone & voice",
        }
        assert client.get("/api/v1/health").status_code == 401
