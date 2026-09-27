import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.main import local_command
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.registry import ToolRegistry


def test_catalog_is_sorted_public_and_does_not_invoke_handlers():
    registry = ToolRegistry()
    handler = AsyncMock()
    registry.register(Tool("z", "last", Input, RiskLevel.SAFE, handler))
    registry.register(Tool("a", "first", Input, RiskLevel.CONFIRM, handler))
    registry.register(
        Tool("hidden", "private", Input, RiskLevel.CONFIRM, handler, expose_to_llm=False)
    )
    assert [item["name"] for item in registry.catalog()] == ["a", "z"]
    assert registry.catalog()[0]["risk"] == "CONFIRM"
    handler.assert_not_called()


@pytest.fixture
def agent(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="local-test")
    result = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    yield result, settings
    result.workflows.close()


async def test_cli_filters_without_llm(agent, capsys):
    service, _ = agent
    await local_command(service, "/tools clipboard")
    result = json.loads(capsys.readouterr().out)
    assert {item["name"] for item in result["tools"]} == {"read_clipboard", "copy_to_clipboard"}
    service.planner.llm.generate_response.assert_not_called()


def test_discovery_endpoints_require_auth_and_exclude_private_tools(agent):
    service, settings = agent
    with TestClient(create_app(settings, service)) as client:
        for path in ("/api/v1/capabilities", "/api/v1/diagnostics"):
            assert client.get(path).status_code == 401
            assert (
                client.get(
                    path,
                    headers={
                        "Authorization": "Bearer local-test",
                        "Origin": "https://evil.example",
                    },
                ).status_code
                == 403
            )
            response = client.get(path, headers={"Authorization": "Bearer local-test"})
            assert response.status_code == 200
            assert "local-test" not in response.text
        tools = client.get(
            "/api/v1/capabilities", headers={"Authorization": "Bearer local-test"}
        ).json()["tools"]
        assert "prune_workflow_records" not in {tool["name"] for tool in tools}
        terminal = next(tool for tool in tools if tool["name"] == "run_terminal_command")
        assert terminal["dynamic_policy"]
        service.planner.llm.generate_response.assert_not_called()
