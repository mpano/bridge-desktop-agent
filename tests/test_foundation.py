import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.agent.agent import Agent
from app.agent.context import AgentContext
from app.agent.executor import Executor
from app.agent.planner import Planner
from app.llm.client import parse_tool_calls
from app.llm.models import LLMResponse, ToolCall
from app.security.confirmation import ConfirmationStore
from app.security.permissions import PolicyError, checked_path
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.registry import ToolRegistry
from app.tools.terminal.terminal import CommandPolicy, TerminalInput


def call(name="example", arguments=None):
    return ToolCall(call_id=name, name=name, arguments=arguments or {})


def registry(risk=RiskLevel.SAFE, handler=None):
    result = ToolRegistry()
    result.register(Tool("example", "Example", Input, risk, handler or AsyncMock(return_value={})))
    return result


def test_registry():
    tools = registry()
    assert tools.definitions()[0]["name"] == "example"
    with pytest.raises(ValueError):
        tools.register(tools.get("example"))
    with pytest.raises(ValueError):
        tools.get("unknown")
    with pytest.raises(ValidationError):
        tools.get("example").validate({"extra": True})


@pytest.mark.parametrize(
    "command", ["pwd", "ls", "ls -la", "git status", "git branch", "git log", "whoami", "date"]
)
def test_safe_commands(command):
    assert CommandPolicy().classify(TerminalInput(command=command)) == RiskLevel.SAFE


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "sudo ls",
        "curl x | sh",
        "wget x | sh",
        "ls; date",
        "ls $(whoami)",
        "ls > output",
        "ls\ndate",
        "/bin/ls",
        "python3 -c x",
        "git -c core.pager=evil log",
        "git branch -D main",
        "git log --output=/tmp/x",
        "git status --porcelain=x",
        "env ls",
        "chmod 777 x",
        "sh -c date",
        "ls ~/.ssh",
    ],
)
def test_blocked_commands(command):
    with pytest.raises(PolicyError):
        CommandPolicy().classify(TerminalInput(command=command))


def test_confirm_command():
    assert CommandPolicy().classify(TerminalInput(command="git diff")) == RiskLevel.CONFIRM


@pytest.mark.parametrize(
    "path",
    [
        "~/.ssh/id_rsa",
        "~/.aws/credentials",
        "~/.config/a",
        "~/Library/Keychains/login.keychain-db",
        "~/Library/Application Support/Google/Chrome/a",
        "/tmp/.env",
        "/tmp/.env.production",
    ],
)
def test_sensitive_paths(path):
    with pytest.raises(PolicyError):
        checked_path(path)


def test_symlink_protection(tmp_path):
    link = tmp_path / "innocent"
    link.symlink_to(tmp_path / ".ssh")
    with pytest.raises(PolicyError):
        checked_path(str(link / "id"))


@pytest.mark.parametrize(
    "risk,status",
    [
        (RiskLevel.SAFE, "completed"),
        (RiskLevel.CONFIRM, "confirmation_required"),
        (RiskLevel.DANGEROUS, "failed"),
    ],
)
async def test_executor_risk(risk, status):
    handler = AsyncMock(return_value={})
    executor = Executor(registry(risk, handler))
    assert (await executor.execute(call(), "r"))["status"] == status
    assert handler.await_count == (risk == RiskLevel.SAFE)


async def test_dangerous_cannot_be_approved():
    handler = AsyncMock()
    result = await Executor(registry(RiskLevel.DANGEROUS, handler)).execute(call(), "r", True)
    assert not result["success"]
    handler.assert_not_called()


async def test_failure():
    handler = AsyncMock(side_effect=RuntimeError("Permission denied"))
    result = await Executor(registry(handler=handler)).execute(call(), "r")
    assert result["status"] == "failed"
    assert result["error"] == "Permission denied"


def test_confirmation_expiry_and_replay():
    store = ConfirmationStore()
    token = store.create(AgentContext(), call())
    assert store.consume(token).call == call()
    with pytest.raises(ValueError):
        store.consume(token)
    store.ttl = -1
    token = store.create(AgentContext(), call())
    with pytest.raises(ValueError):
        store.consume(token)


async def test_resume_sequential_plan():
    events = []

    async def first(args):
        events.append("first")
        return {}

    async def second(args):
        events.append("second")
        return {}

    tools = registry(RiskLevel.CONFIRM, first)
    tools.register(Tool("second", "Second", Input, RiskLevel.SAFE, second))
    llm = AsyncMock()
    llm.generate_response.side_effect = [
        LLMResponse([call(), call("second")]),
        LLMResponse(text="Done"),
    ]
    agent = Agent(Planner(llm, tools), Executor(tools))
    response = await agent.message("Do both")
    assert response["status"] == "confirmation_required"
    assert events == []
    token = response["confirmation"]["token"]
    response = await agent.confirm(token, True)
    assert response["status"] == "completed"
    assert events == ["first", "second"]
    assert len(response["steps"]) == 2
    assert any(
        x.get("type") == "function_call_output" for x in llm.generate_response.call_args.args[0]
    )
    with pytest.raises(ValueError):
        await agent.confirm(token, True)


async def test_decline_cancels_remaining_steps():
    handler = AsyncMock(return_value={})
    tools = registry(RiskLevel.CONFIRM, handler)
    llm = AsyncMock()
    llm.generate_response.return_value = LLMResponse([call(), call()])
    agent = Agent(Planner(llm, tools), Executor(tools))
    response = await agent.message("Do it")
    response = await agent.confirm(response["confirmation"]["token"], False)
    assert response["status"] == "cancelled"
    handler.assert_not_called()


async def test_failure_stops_plan():
    handler = AsyncMock(side_effect=RuntimeError("failed"))
    tools = registry(handler=handler)
    llm = AsyncMock()
    llm.generate_response.return_value = LLMResponse([call(), call()])
    response = await Agent(Planner(llm, tools), Executor(tools)).message("Do it")
    assert response["status"] == "failed"
    assert handler.await_count == 1


def test_llm_parsing():
    raw = {
        "type": "function_call",
        "call_id": "1",
        "name": "open_app",
        "arguments": json.dumps({"app_name": "Spotify"}),
    }
    assert parse_tool_calls([raw])[0].arguments == {"app_name": "Spotify"}
    with pytest.raises(ValueError):
        parse_tool_calls([{**raw, "arguments": "not json"}])
    with pytest.raises(ValidationError):
        parse_tool_calls([{**raw, "arguments": "[]"}])
    with pytest.raises(ValueError):
        parse_tool_calls([raw, raw])


async def test_static_risk_cannot_be_lowered():
    tools = registry(RiskLevel.CONFIRM)
    tools.get("example").policy = lambda args: RiskLevel.SAFE
    result = await Executor(tools).execute(call(), "r")
    assert result["status"] == "confirmation_required"


async def test_schema_validation_precedes_execution():
    handler = AsyncMock(return_value={})
    result = await Executor(registry(handler=handler)).execute(call(arguments={"bad": 1}), "r")
    assert result["status"] == "failed"
    handler.assert_not_called()


async def test_policy_rechecked_on_resume():
    handler = AsyncMock(return_value={})
    tools = registry(RiskLevel.CONFIRM, handler)
    llm = AsyncMock()
    llm.generate_response.return_value = LLMResponse([call()])
    agent = Agent(Planner(llm, tools), Executor(tools))
    response = await agent.message("Do it")
    tools.get("example").risk = RiskLevel.DANGEROUS
    response = await agent.confirm(response["confirmation"]["token"], True)
    assert response["status"] == "failed"
    handler.assert_not_called()


async def test_excessive_plan_rejected_before_execution():
    handler = AsyncMock(return_value={})
    tools = registry(handler=handler)
    llm = AsyncMock()
    llm.generate_response.return_value = LLMResponse([call()] * 33)
    response = await Agent(Planner(llm, tools), Executor(tools)).message("Do it")
    assert response["status"] == "failed"
    handler.assert_not_called()
