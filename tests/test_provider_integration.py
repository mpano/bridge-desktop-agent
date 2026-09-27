import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import ValidationError

from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm.factory import create_llm
from app.llm.models import LLMResponse, ToolCall
from app.llm.ollama import OllamaLLMClient
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


@pytest.mark.parametrize(
    "provider,mode,allow,disclosed",
    [
        ("openai", "status_only", [], False),
        ("openai", "allowlist", ["private_result"], True),
        ("openai", "allowlist", ["get_volume"], False),
        ("openai", "all", [], True),
        ("ollama", "status_only", [], True),
    ],
)
async def test_full_agent_filters_tool_results_and_later_history(
    tmp_path,
    provider,
    mode,
    allow,
    disclosed,
):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db",
        llm_provider=provider,
        remote_tool_results=mode,
        remote_tool_result_allowlist=allow,
    )
    llm = AsyncMock()
    call = ToolCall(call_id="one", name="private_result", arguments={})
    llm.generate_response.side_effect = [
        LLMResponse(calls=[call]),
        LLMResponse(text="Done"),
        LLMResponse(text="Next"),
    ]
    agent = build_agent(settings, llm=llm, runner=AsyncMock())
    agent.executor.registry.register(
        Tool(
            "private_result",
            "Test",
            Input,
            RiskLevel.SAFE,
            AsyncMock(return_value={"nested": {"secret": "CANARY_PRIVATE"}}),
        )
    )
    try:
        result = await agent.message("Run the tool")
        assert "CANARY_PRIVATE" in json.dumps(result["steps"])
        after_tool = llm.generate_response.call_args_list[1].args[0]
        assert ("CANARY_PRIVATE" in json.dumps(after_tool)) is disclosed
        await agent.message("Another request")
        later = llm.generate_response.call_args_list[2].args[0]
        assert ("CANARY_PRIVATE" in json.dumps(later)) is disclosed
    finally:
        await agent.close()


async def test_local_provider_full_tool_loop_requires_confirmation(tmp_path):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        assert request.url.host == "127.0.0.1"
        assert "authorization" not in request.headers
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["tools"]})
        if len([path for path, _ in requests if path == "/api/chat"]) == 1:
            return httpx.Response(
                200,
                json={
                    "done": True,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "approved_test",
                                    "arguments": {},
                                }
                            }
                        ],
                    },
                },
            )
        assert body["messages"][-1]["role"] == "tool"
        assert "LOCAL_ONLY_RESULT" in body["messages"][-1]["content"]
        return httpx.Response(
            200,
            json={
                "done": True,
                "message": {
                    "role": "assistant",
                    "content": "Finished",
                },
            },
        )

    settings = Settings(_env_file=None, database_path=tmp_path / "db", llm_provider="ollama")
    local = OllamaLLMClient(settings, transport=httpx.MockTransport(respond))
    handler = AsyncMock(return_value={"message": "LOCAL_ONLY_RESULT"})
    agent = build_agent(settings, llm=local, runner=AsyncMock())
    agent.executor.registry.register(
        Tool(
            "approved_test",
            "Test approval",
            Input,
            RiskLevel.CONFIRM,
            handler,
        )
    )
    try:
        result = await agent.message("Run approved test")
        assert result["status"] == "confirmation_required"
        handler.assert_not_called()
        result = await agent.confirm(result["confirmation"]["token"], True)
        assert result["status"] == "completed"
        assert result["message"] == "Finished"
        handler.assert_awaited_once()
    finally:
        await agent.close()


async def test_local_factory_never_constructs_openai_client():
    settings = Settings(_env_file=None, llm_provider="ollama", openai_api_key="")
    with patch("app.llm.factory.OpenAILLMClient", side_effect=AssertionError("remote forbidden")):
        client = create_llm(settings)
        assert isinstance(client, OllamaLLMClient)
        await client.close()


@pytest.mark.parametrize("arguments", [{"command": "sudo ls"}, {"command": "rm -rf /"}])
async def test_local_model_cannot_bypass_terminal_policy(tmp_path, arguments):
    def respond(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["tools"]})
        return httpx.Response(
            200,
            json={
                "done": True,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "run_terminal_command",
                                "arguments": arguments,
                            }
                        }
                    ],
                },
            },
        )

    settings = Settings(_env_file=None, database_path=tmp_path / "db", llm_provider="ollama")
    runner = AsyncMock()
    agent = build_agent(
        settings,
        runner=runner,
        llm=OllamaLLMClient(
            settings,
            transport=httpx.MockTransport(respond),
        ),
    )
    try:
        result = await agent.message("A blocked request")
        assert result["status"] == "failed"
        runner.run.assert_not_called()
    finally:
        await agent.close()


async def test_local_connection_failure_never_runs_tools_or_falls_back(tmp_path):
    def respond(request):
        raise httpx.ConnectError("PRIVATE_DETAILS", request=request)

    settings = Settings(_env_file=None, database_path=tmp_path / "db", llm_provider="ollama")
    runner = AsyncMock()
    with patch("app.llm.factory.OpenAILLMClient", side_effect=AssertionError("no fallback")):
        agent = build_agent(
            settings,
            runner=runner,
            llm=OllamaLLMClient(
                settings,
                transport=httpx.MockTransport(respond),
            ),
        )
        try:
            result = await agent.message("Open Spotify")
            assert result["status"] == "failed"
            assert "Start Ollama" in result["message"]
            assert "PRIVATE_DETAILS" not in json.dumps(result)
            runner.run.assert_not_called()
        finally:
            await agent.close()


@pytest.mark.parametrize(
    "values",
    [
        {"llm_provider": "unknown"},
        {"local_llm_base_url": "http://example.com:11434"},
        {"local_llm_model": "model:cloud"},
        {"remote_tool_results": "off"},
        {"remote_tool_result_allowlist": ["*"]},
    ],
)
def test_invalid_provider_configuration(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
