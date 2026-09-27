from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.memory.store import SQLiteMemory


@pytest.fixture
def ui(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db",
        api_token="local-test-token",
        openai_api_key="private-provider-secret",
    )
    runner = AsyncMock()
    runner.run.return_value = ""
    llm = AsyncMock()
    agent = build_agent(settings, llm=llm, runner=runner)
    with TestClient(
        create_app(settings, agent, enable_ui=True), base_url="http://127.0.0.1:8000"
    ) as client:
        yield client, agent, runner, llm, settings


def headers(origin="http://127.0.0.1:8000"):
    return {
        "Authorization": "Bearer local-test-token",
        "Origin": origin,
        "Sec-Fetch-Site": "same-origin",
    }


def test_assets_and_security_headers(ui):
    client, _, _, _, _ = ui
    response = client.get("/")
    assert response.status_code == 200
    assert "Bridge" in response.text
    assert "private-provider-secret" not in response.text
    assert "local-test-token" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    for path in ("/ui/app.js", "/ui/style.css", "/ui/bridge-logo.png"):
        assert client.get(path).status_code == 200
    assert client.get("/ui/.env").status_code == 404
    assert client.get("/api/v1/preferences").status_code == 401
    assert client.get("/api/v1/preferences", headers=headers()).json() == {
        "default_browser": "Google Chrome",
        "default_editor": "Visual Studio Code",
    }


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "https://evil.example",
        "http://localhost:8000",
        "http://127.0.0.1:8001",
        "http://127.0.0.1:8000.evil.example",
        "https://127.0.0.1:8000",
    ],
)
def test_foreign_origins_rejected(ui, origin):
    client, _, _, _, _ = ui
    assert client.get("/api/v1/preferences", headers=headers(origin)).status_code == 403


def test_cross_site_fetch_and_foreign_host_rejected(ui):
    client, _, _, _, _ = ui
    assert (
        client.get(
            "/api/v1/preferences",
            headers={"Authorization": "Bearer local-test-token", "Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_alias_ui_mutations_use_confirmation(ui, tmp_path):
    client, agent, _, llm, _ = ui
    response = client.post(
        "/api/v1/projects", headers=headers(), json={"name": "NLP", "path": str(tmp_path)}
    )
    data = response.json()
    assert data["status"] == "confirmation_required"
    assert agent.list_projects() == []
    result = client.post(
        "/api/v1/agent/confirm",
        headers=headers(),
        json={"token": data["confirmation"]["token"], "approved": True},
    ).json()
    assert result["status"] == "completed"
    assert agent.list_projects() == [{"name": "nlp", "path": str(tmp_path)}]
    response = client.post(
        "/api/v1/projects/forget", headers=headers(), json={"name": "nlp"}
    ).json()
    assert response["status"] == "confirmation_required"
    client.post(
        "/api/v1/agent/confirm",
        headers=headers(),
        json={"token": response["confirmation"]["token"], "approved": False},
    )
    assert len(agent.list_projects()) == 1
    llm.generate_response.assert_not_called()


def test_preferences_apply_atomically_after_approval(ui):
    client, agent, _, llm, settings = ui
    response = client.post(
        "/api/v1/preferences",
        headers=headers(),
        json={"default_browser": "Safari", "default_editor": "GoLand"},
    ).json()
    assert response["status"] == "confirmation_required"
    assert agent.get_preferences()["default_browser"] == "Google Chrome"
    client.post(
        "/api/v1/agent/confirm",
        headers=headers(),
        json={"token": response["confirmation"]["token"], "approved": True},
    )
    assert agent.get_preferences() == {"default_browser": "Safari", "default_editor": "GoLand"}
    memory = SQLiteMemory(settings.database_path)
    assert memory.get("default_editor").value == "GoLand"
    llm.generate_response.assert_not_called()
    assert (
        "private-provider-secret" not in client.get("/api/v1/preferences", headers=headers()).text
    )
    assert (
        client.post(
            "/api/v1/preferences",
            headers=headers(),
            json={"default_browser": "Mail", "default_editor": "bad\nname"},
        ).status_code
        == 422
    )
    assert agent.get_preferences()["default_browser"] == "Safari"


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        ("/api/v1/projects", {"name": "bad", "path": "~/.ssh"}),
        (
            "/api/v1/preferences",
            {
                "default_browser": "Chrome",
                "default_editor": "VS Code",
                "openai_api_key": "do-not-store",
            },
        ),
    ],
)
def test_management_rejects_protected_paths_and_secret_fields(ui, endpoint, payload):
    client, _, _, _, _ = ui
    assert client.post(endpoint, headers=headers(), json=payload).status_code in {400, 422}


async def test_dynamic_preferences_and_frozen_plans(tmp_path):
    from app.llm.models import ToolCall

    settings = Settings(_env_file=None, database_path=tmp_path / "db")
    runner = AsyncMock()
    runner.run.return_value = ""
    agent = build_agent(settings, llm=AsyncMock(), runner=runner)
    try:
        original = agent._prepare(
            [ToolCall(call_id="old", name="open_project", arguments={"path": str(tmp_path)})]
        )[0]
        result = await agent.request_tool(
            "set_preferences", {"default_browser": "Safari", "default_editor": "GoLand"}
        )
        await agent.confirm(result["confirmation"]["token"], True)
        await agent.request_tool("open_project", {"path": str(tmp_path)})
        assert runner.run.call_args.args == ("/usr/bin/open", "-a", "GoLand", str(tmp_path))
        await agent.request_tool("open_url", {"url": "https://github.com"})
        assert runner.run.call_args.args[2] == "Safari"
        await agent.executor.execute(original, "r")
        assert runner.run.call_args.args[2] == "Visual Studio Code"
    finally:
        await agent.close()


def test_api_mode_remains_browser_restricted(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="local-test-token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    with TestClient(create_app(settings, agent), base_url="http://127.0.0.1:8000") as client:
        assert client.get("/").status_code == 404
        assert client.get("/ui/app.js").status_code == 404
        assert client.get("/api/v1/preferences", headers=headers()).status_code == 403
        assert (
            client.get(
                "/api/v1/preferences", headers={"Authorization": "Bearer local-test-token"}
            ).status_code
            == 200
        )
