import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.agent.agent import Agent
from app.agent.context import AgentContext
from app.agent.executor import Executor
from app.agent.planner import Planner
from app.api.server import create_app
from app.config.settings import Settings
from app.llm.models import LLMResponse, ToolCall
from app.memory.store import SQLiteMemory
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.files import projects
from app.tools.registry import ToolRegistry
from app.tools.system.discovery import ApplicationCatalog
from app.workflows.store import SQLiteWorkflows


def call(tool="safe", **arguments):
    return ToolCall(call_id=tool, name=tool, arguments=arguments)


def make_agent(path: Path):
    safe = AsyncMock(return_value={"output": "private tool output"})
    confirm = AsyncMock(return_value={})
    registry = ToolRegistry()
    registry.register(Tool("safe", "Safe", Input, RiskLevel.SAFE, safe))
    registry.register(Tool("confirm", "Confirm", Input, RiskLevel.CONFIRM, confirm))
    llm = AsyncMock()
    llm.generate_response.side_effect = [
        LLMResponse([call(), call("confirm"), call()]),
        LLMResponse(text="Done"),
    ]
    return (
        Agent(
            Planner(llm, registry),
            Executor(registry),
            workflows=SQLiteWorkflows(path, can_persist=lambda call: True),
        ),
        safe,
        confirm,
    )


async def test_alias_lifecycle_and_confirmation(tmp_path):
    registry = ToolRegistry()
    path = tmp_path / "projects.db"
    memory = SQLiteMemory(path)
    projects.register(registry, memory)
    executor = Executor(registry)
    remember = call("remember_project", name=" NLP   Project ", path=str(tmp_path))
    result = await executor.execute(remember, "r")
    assert result["status"] == "confirmation_required"
    assert memory.projects() == []
    assert (await executor.execute(remember, "r", approved=True))["success"]
    # Recreate repository to verify persistence rather than in-memory caching.
    projects.register(ToolRegistry(), SQLiteMemory(path))
    assert SQLiteMemory(path).get("project:nlp project").value == str(tmp_path)
    result = await executor.execute(call("lookup_project", name="NLP PROJECT"), "r")
    assert result["result"]["path"] == str(tmp_path)
    result = await executor.execute(call("list_projects"), "r")
    assert result["result"]["projects"][0]["name"] == "nlp project"
    forget = call("forget_project", name="nlp project")
    assert (await executor.execute(forget, "r"))["status"] == "confirmation_required"
    assert (await executor.execute(forget, "r", approved=True))["success"]
    assert tmp_path.is_dir()
    assert not (await executor.execute(call("lookup_project", name="nlp project"), "r"))["success"]


async def test_alias_protected_and_missing_paths(tmp_path):
    registry = ToolRegistry()
    projects.register(registry, SQLiteMemory(tmp_path / "memory.db"))
    executor = Executor(registry)
    for path in ["~/.ssh", str(tmp_path / "missing")]:
        result = await executor.execute(
            call("remember_project", name="alias", path=path), "r", approved=True
        )
        assert result["status"] == "failed"


def test_application_discovery(tmp_path):
    (tmp_path / "GoLand.app").mkdir()
    (tmp_path / "PyCharm.app").mkdir()
    (tmp_path / "Utilities").mkdir()
    (tmp_path / "Utilities" / "Terminal.app").mkdir()
    (tmp_path / "Hidden.app").symlink_to(tmp_path / "GoLand.app")
    catalog = ApplicationCatalog([tmp_path])
    assert [a["name"] for a in catalog.search("goland")["applications"]] == ["GoLand"]
    assert {a["name"] for a in catalog.search("")["applications"]} == {
        "GoLand",
        "PyCharm",
        "Terminal",
    }
    assert catalog.search("unknown")["applications"] == []


async def test_restart_resume_requires_fresh_confirmation(tmp_path):
    path = tmp_path / "workflows.db"
    first, safe, confirm = make_agent(path)
    response = await first.message("private user message")
    request_id = response["request_id"]
    old_token = response["confirmation"]["token"]
    assert safe.await_count == 1
    assert confirm.await_count == 0
    await first.close()

    second, safe, confirm = make_agent(path)
    assert second.get_workflow(request_id)["status"] == "paused"
    with pytest.raises(ValueError):
        await second.confirm(old_token, True)
    response = await second.resume_workflow(request_id)
    assert response["status"] == "confirmation_required"
    assert response["confirmation"]["token"] != old_token
    assert safe.await_count == 0
    response = await second.confirm(response["confirmation"]["token"], True)
    assert response["status"] == "completed"
    assert safe.await_count == 1  # Only the remaining safe step ran.
    assert confirm.await_count == 1
    second.planner.llm.generate_response.assert_not_called()
    with pytest.raises(ValueError):
        await second.resume_workflow(request_id)
    await second.close()


async def test_inflight_action_never_replayed(tmp_path):
    path = tmp_path / "workflows.db"
    store = SQLiteWorkflows(path, can_persist=lambda call: True)
    context = AgentContext(queue=[call()])
    store.save(context, "running", in_flight=True)
    store.close()
    agent, safe, _ = make_agent(path)
    assert agent.get_workflow(context.request_id)["status"] == "interrupted"
    with pytest.raises(ValueError, match="uncertain"):
        await agent.resume_workflow(context.request_id)
    safe.assert_not_called()
    assert (await agent.cancel_workflow(context.request_id))["status"] == "cancelled"
    await agent.close()


async def test_completed_step_is_preserved_on_crash_between_steps(tmp_path):
    path = tmp_path / "workflows.db"
    store = SQLiteWorkflows(path, can_persist=lambda call: True)
    context = AgentContext(
        queue=[call()],
        steps=[
            {
                "tool": "safe",
                "call_id": "old",
                "success": True,
                "status": "completed",
                "result": {"output": "private result"},
            }
        ],
    )
    store.save(context, "running")
    store.close()
    agent, safe, _ = make_agent(path)
    response = await agent.resume_workflow(context.request_id)
    assert response["status"] == "completed"
    assert safe.await_count == 1
    assert len(response["steps"]) == 2
    await agent.close()


async def test_resume_invalidates_old_pending_token_and_cancel(tmp_path):
    agent, _, confirm = make_agent(tmp_path / "db")
    response = await agent.message("Do it")
    token = response["confirmation"]["token"]
    request_id = response["request_id"]
    response = await agent.resume_workflow(request_id)
    with pytest.raises(ValueError):
        await agent.confirm(token, True)
    await agent.cancel_workflow(request_id)
    with pytest.raises(ValueError):
        await agent.confirm(response["confirmation"]["token"], True)
    confirm.assert_not_called()
    await agent.close()


async def test_journal_omits_conversations_outputs_and_tokens(tmp_path):
    path = tmp_path / "db"
    agent, _, _ = make_agent(path)
    response = await agent.message("private user message")
    with sqlite3.connect(path) as db:
        raw = json.dumps(db.execute("SELECT * FROM workflows").fetchall())
    assert "private user message" not in raw
    assert "private tool output" not in raw
    assert response["confirmation"]["token"] not in raw
    await agent.close()


def test_single_process_lock_and_release(tmp_path):
    path = tmp_path / "db"
    first = SQLiteWorkflows(path, can_persist=lambda call: True)
    with pytest.raises(RuntimeError, match="Another Bridge"):
        SQLiteWorkflows(path, can_persist=lambda call: True)
    first.close()
    second = SQLiteWorkflows(path, can_persist=lambda call: True)
    second.close()


async def test_changed_policy_blocks_recovery(tmp_path):
    path = tmp_path / "db"
    store = SQLiteWorkflows(path, can_persist=lambda call: True)
    context = AgentContext(queue=[call()])
    store.save(context, "running")
    store.close()
    agent, safe, _ = make_agent(path)
    agent.executor.registry.get("safe").risk = RiskLevel.DANGEROUS
    with pytest.raises(ValueError):
        await agent.resume_workflow(context.request_id)
    safe.assert_not_called()
    await agent.close()


async def test_reset_clears_only_conversation(tmp_path):
    agent, _, _ = make_agent(tmp_path / "db")
    agent.history = [{"role": "user", "content": "Old question"}]
    response = await agent.message("New request")
    agent.reset_conversation()
    assert not agent.history
    assert agent.get_workflow(response["request_id"])["status"] == "confirmation_required"
    await agent.close()


def test_workflow_api_auth_and_recovery(tmp_path):
    path = tmp_path / "db"
    store = SQLiteWorkflows(path, can_persist=lambda call: True)
    context = AgentContext(queue=[call("confirm")])
    store.save(context, "running")
    store.close()
    agent, _, confirm = make_agent(path)
    settings = Settings(_env_file=None, database_path=path, api_token="test-token")
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(create_app(settings, agent)) as client:
        url = f"/api/v1/workflows/{context.request_id}"
        assert client.get("/api/v1/workflows").status_code == 401
        assert client.post(url + "/resume").status_code == 401
        assert client.get(url, headers=headers).json()["status"] == "paused"
        response = client.post(url + "/resume", headers=headers).json()
        assert response["status"] == "confirmation_required"
        token = response["confirmation"]["token"]
        response = client.post(
            "/api/v1/agent/confirm", headers=headers, json={"token": token, "approved": True}
        )
        assert response.json()["status"] == "completed"
        assert client.post(url + "/resume", headers=headers).status_code == 409
        assert client.post("/api/v1/conversation/reset", headers=headers).status_code == 200
        confirm.assert_awaited_once()


def test_unreviewed_arguments_are_not_persisted(tmp_path):
    path = tmp_path / "db"
    store = SQLiteWorkflows(path)
    context = AgentContext(queue=[call("open_url", url="https://example.com/?token=secret")])
    store.save(context, "running")
    saved = store.get(context.request_id)
    assert not saved["recoverable"]
    assert saved["queue"][0]["arguments_omitted"]
    with sqlite3.connect(path) as db:
        assert "secret" not in db.execute("SELECT snapshot FROM workflows").fetchone()[0]
    store.close()
    store = SQLiteWorkflows(path)
    assert store.get(context.request_id)["status"] == "interrupted"
    store.close()


async def test_failed_checkpoint_after_operation_blocks_replay(tmp_path, monkeypatch):
    path = tmp_path / "db"
    agent, safe, _ = make_agent(path)
    original = agent.workflows.save

    def fail_after_operation(context, status, **kwargs):
        if context.steps:
            raise sqlite3.OperationalError("disk full")
        return original(context, status, **kwargs)

    monkeypatch.setattr(agent.workflows, "save", fail_after_operation)
    response = await agent.message("Do it")
    assert response["status"] == "failed"
    assert "uncertain" in response["message"]
    safe.assert_awaited_once()
    await agent.close()
    recovered, safe, _ = make_agent(path)
    assert recovered.get_workflow(response["request_id"])["status"] == "interrupted"
    with pytest.raises(ValueError):
        await recovered.resume_workflow(response["request_id"])
    safe.assert_not_called()
    await recovered.close()


async def test_project_alias_through_real_composition(tmp_path):
    from app.bootstrap import build_agent

    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db",
        remote_tool_results="allowlist",
        remote_tool_result_allowlist=["lookup_project"],
    )
    llm = AsyncMock()
    llm.generate_response.side_effect = [
        LLMResponse([call("remember_project", name="NLP", path=str(tmp_path))]),
        LLMResponse(text="Saved."),
        LLMResponse([call("lookup_project", name="nlp")]),
        LLMResponse([call("open_project", path=str(tmp_path))]),
        LLMResponse(text="Opened."),
    ]
    runner = AsyncMock()
    runner.run.return_value = ""
    agent = build_agent(settings, llm=llm, runner=runner)
    result = await agent.message("Remember my NLP project")
    assert result["status"] == "confirmation_required"
    result = await agent.confirm(result["confirmation"]["token"], True)
    assert result["status"] == "completed"
    result = await agent.message("Open my NLP project")
    assert result["status"] == "completed"
    assert runner.run.call_args.args == ("/usr/bin/open", "-a", "Visual Studio Code", str(tmp_path))
    llm_history = llm.generate_response.call_args.args[0]
    assert any(str(tmp_path) in item.get("output", "") for item in llm_history)
    assert not agent.executor.registry.get("open_url").persist_arguments
    await agent.close()
