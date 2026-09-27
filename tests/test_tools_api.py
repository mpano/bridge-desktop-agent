from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm.models import LLMResponse, ToolCall
from app.memory.models import Preference
from app.memory.store import SQLiteMemory
from app.tools.browser.browser import BrowserController
from app.tools.macos.applescript import MacOSAppleScript


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_path=tmp_path / "memory.db",
        screenshot_directory=tmp_path / "screenshots",
        api_token="test-token",
    )


async def test_multistep_native_calls(settings):
    runner = AsyncMock()
    runner.run.return_value = ""
    llm = AsyncMock()
    llm.generate_response.side_effect = [
        LLMResponse(
            [
                ToolCall(call_id="1", name="open_app", arguments={"app_name": "Spotify"}),
                ToolCall(
                    call_id="2",
                    name="open_url",
                    arguments={"url": "https://github.com", "browser": "Chrome"},
                ),
            ]
        ),
        LLMResponse(text="Opened Spotify and GitHub."),
    ]
    agent = build_agent(settings, llm=llm, runner=runner)
    response = await agent.message("Open Spotify and then open GitHub in Chrome.")
    assert response["status"] == "completed"
    assert [c.args for c in runner.run.await_args_list] == [
        ("/usr/bin/open", "-a", "Spotify"),
        ("/usr/bin/open", "-a", "Google Chrome", "https://github.com"),
    ]


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "javascript:alert(1)", "https://user:password@example.com", "https://"],
)
async def test_url_policy(url):
    runner = AsyncMock()
    with pytest.raises(ValueError):
        await BrowserController(runner, "Chrome").open_url(url)
    runner.run.assert_not_called()


async def test_screenshot_requires_real_output(settings):
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    result = await agent.executor.execute(
        ToolCall(call_id="1", name="take_screenshot", arguments={}), "request"
    )
    assert not result["success"]


async def test_terminal_uses_clean_environment(settings, tmp_path):
    runner = AsyncMock()
    runner.run.return_value = "clean"
    agent = build_agent(settings, llm=AsyncMock(), runner=runner)
    result = await agent.executor.execute(
        ToolCall(
            call_id="1",
            name="run_terminal_command",
            arguments={"command": "git status", "cwd": str(tmp_path)},
        ),
        "request",
    )
    assert result["success"]
    invocation = runner.run.call_args
    assert invocation.args[0] == "/usr/bin/git"
    assert "core.fsmonitor=false" in invocation.args
    assert "OPENAI_API_KEY" not in invocation.kwargs["env"]


def test_applescript_escaping():
    assert MacOSAppleScript.literal('a"b') == '"a\\"b"'
    with pytest.raises(ValueError):
        MacOSAppleScript.literal("a\nb")


def test_memory(settings):
    memory = SQLiteMemory(settings.database_path)
    memory.set(Preference("default_browser", "Google Chrome"))
    assert memory.get("default_browser").value == "Google Chrome"
    memory.set(Preference("default_browser", "Safari"))
    assert memory.get("default_browser").value == "Safari"
    with pytest.raises(ValueError):
        memory.set(Preference("api_token", "secret"))


def test_api_auth_and_confirmation(settings):
    agent = AsyncMock()
    agent.planner.llm = AsyncMock()
    agent.message.return_value = {
        "status": "completed",
        "request_id": "r",
        "message": "Done",
        "steps": [],
    }
    agent.confirm.side_effect = ValueError("Already used")
    with TestClient(create_app(settings, agent)) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/api/v1/agent/message", json={"message": "hi"}).status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        assert (
            client.post(
                "/api/v1/agent/message", headers=headers, json={"message": "hi"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/agent/message",
                headers={**headers, "Origin": "https://evil"},
                json={"message": "hi"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/agent/confirm", headers=headers, json={"token": "x", "approved": True}
            ).status_code
            == 409
        )


@pytest.mark.parametrize(
    "name,arguments,expected",
    [
        ("open_app", {"app_name": "Chrome"}, ("/usr/bin/open", "-a", "Google Chrome")),
        ("open_app", {"app_name": "Mail"}, ("/usr/bin/open", "-a", "Mail")),
        ("open_app", {"app_name": "VS Code"}, ("/usr/bin/open", "-a", "Visual Studio Code")),
    ],
)
async def test_application_milestones(settings, name, arguments, expected):
    runner = AsyncMock()
    runner.run.return_value = ""
    agent = build_agent(settings, llm=AsyncMock(), runner=runner)
    result = await agent.executor.execute(
        ToolCall(call_id="1", name=name, arguments=arguments), "r"
    )
    assert result["success"]
    assert runner.run.call_args.args == expected


async def test_folders_projects_volume_and_processes(settings, tmp_path):
    runner = AsyncMock()
    runner.run.return_value = ""
    agent = build_agent(settings, llm=AsyncMock(), runner=runner)
    for name in ("open_folder", "open_project"):
        result = await agent.executor.execute(
            ToolCall(call_id=name, name=name, arguments={"path": str(tmp_path)}), "r"
        )
        assert result["success"]
        assert runner.run.call_args.args[-1] == str(tmp_path)
    runner.run.side_effect = ["", "30", "Finder, Spotify"]
    result = await agent.executor.execute(
        ToolCall(call_id="v", name="set_volume", arguments={"level": 30}), "r"
    )
    assert result["result"]["level"] == 30
    result = await agent.executor.execute(
        ToolCall(call_id="p", name="list_running_apps", arguments={}), "r"
    )
    assert result["result"]["applications"] == ["Finder", "Spotify"]


async def test_screenshot_success(settings):
    from pathlib import Path

    runner = AsyncMock()

    async def capture(*argv):
        Path(argv[-1]).write_bytes(b"mock image output")
        return ""

    runner.run.side_effect = capture
    agent = build_agent(settings, llm=AsyncMock(), runner=runner)
    result = await agent.executor.execute(
        ToolCall(call_id="s", name="take_screenshot", arguments={}), "r"
    )
    assert result["success"]
    assert Path(result["result"]["path"]).parent == settings.screenshot_directory
    assert Path(result["result"]["path"]).stat().st_mode & 0o777 == 0o600


async def test_openai_adapter(settings):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.llm.client import OpenAILLMClient

    settings.openai_api_key = __import__("pydantic").SecretStr("test-key")
    client = OpenAILLMClient(settings)
    client._client = AsyncMock()
    item = Mock()
    item.model_dump.return_value = {
        "type": "function_call",
        "call_id": "1",
        "name": "open_app",
        "arguments": '{"app_name":"Spotify"}',
    }
    client._client.responses.create.return_value = SimpleNamespace(output=[item], output_text="")
    response = await client.generate_response([{"role": "user", "content": "Open Spotify"}], [])
    assert response.calls[0].name == "open_app"
    kwargs = client._client.responses.create.call_args.kwargs
    assert kwargs["store"] is False
    assert kwargs["parallel_tool_calls"] is False
    await client.close()
    client._client.close.assert_awaited_once()
