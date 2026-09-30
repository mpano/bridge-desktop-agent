import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm.models import LLMResponse, ToolCall
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


@pytest.mark.parametrize(
    "mode,allow,disclosed",
    [
        ("status_only", [], False),
        ("allowlist", ["private_result"], True),
        ("allowlist", ["get_volume"], False),
        ("all", [], True),
    ],
)
async def test_full_agent_filters_tool_results_and_later_history(
    tmp_path,
    mode,
    allow,
    disclosed,
):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db",
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


@pytest.mark.parametrize(
    "values",
    [
        {"remote_tool_results": "off"},
        {"remote_tool_result_allowlist": ["*"]},
    ],
)
def test_invalid_provider_configuration(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
