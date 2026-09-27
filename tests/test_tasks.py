import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm.models import LLMResponse, ToolCall
from app.main import local_command
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


@pytest.fixture
def setup(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="test")
    llm = AsyncMock()
    agent = build_agent(settings, llm=llm, runner=AsyncMock())
    yield agent, llm, settings
    agent.workflows.close()


def call(name="controlled"):
    return ToolCall(call_id=name, name=name, arguments={})


async def test_stop_during_action_records_outcome_and_skips_remaining(setup):
    agent, llm, _ = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def action(_):
        entered.set()
        await release.wait()
        return {"done": True}

    agent.executor.registry.register(Tool("controlled", "test", Input, RiskLevel.SAFE, action))
    llm.generate_response.return_value = LLMResponse(calls=[call(), call()])
    request_id = agent.submit("two actions")["request_id"]
    await asyncio.wait_for(entered.wait(), 2)
    progress = agent.task_progress(request_id)
    assert progress["current_tool"] == "controlled"
    assert progress["remaining_tools"] == ["controlled"]
    with pytest.raises(ValueError, match="already active"):
        agent.submit("another")
    assert agent.stop_task(request_id)["cancel_requested"]
    assert not agent.background.done()
    release.set()
    await agent.background
    progress = agent.task_progress(request_id)
    assert progress["status"] == "cancelled"
    assert progress["completed_steps"] == 1
    assert progress["result"]["steps"][0]["success"]
    assert agent.get_workflow(request_id)["status"] == "cancelled"
    llm.generate_response.assert_awaited_once()


async def test_cancel_during_planning_never_executes_generated_plan(setup):
    agent, llm, _ = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def plan(*_):
        entered.set()
        await release.wait()
        return LLMResponse(calls=[call()])

    llm.generate_response.side_effect = plan
    handler = AsyncMock(return_value={})
    agent.executor.registry.register(Tool("controlled", "test", Input, RiskLevel.SAFE, handler))
    request_id = agent.submit("request")["request_id"]
    await asyncio.wait_for(entered.wait(), 2)
    agent.stop_task(request_id)
    release.set()
    await agent.background
    assert agent.task_progress(request_id)["status"] == "cancelled"
    handler.assert_not_called()


async def test_queued_cancel_and_shutdown(setup):
    agent, llm, _ = setup
    await agent.lock.acquire()
    request_id = agent.submit("request")["request_id"]
    assert agent.stop_task(request_id)["status"] == "queued"
    agent.lock.release()
    await agent.close()
    assert agent.task_progress(request_id)["status"] == "cancelled"
    llm.generate_response.assert_not_called()


async def test_approval_still_required_and_result_updates(setup):
    agent, llm, _ = setup
    handler = AsyncMock(return_value={})
    agent.executor.registry.register(Tool("controlled", "test", Input, RiskLevel.CONFIRM, handler))
    llm.generate_response.side_effect = [LLMResponse(calls=[call()]), LLMResponse(text="done")]
    request_id = agent.submit("request")["request_id"]
    await agent.background
    result = agent.task_progress(request_id)["result"]
    assert result["status"] == "confirmation_required"
    handler.assert_not_called()
    with pytest.raises(ValueError, match="no longer running"):
        agent.stop_task(request_id)
    await agent.confirm(result["confirmation"]["token"], True)
    assert agent.task_progress(request_id)["status"] == "completed"
    handler.assert_awaited_once()


async def test_cli_collects_result(setup, capsys):
    agent, llm, _ = setup
    llm.generate_response.return_value = LLMResponse(text="done")
    await local_command(agent, "/start hello")
    await agent.background
    request_id = next(iter(agent.tasks))
    result = await local_command(agent, f"/task {request_id}")
    assert result["message"] == "done"
    assert request_id in capsys.readouterr().out


async def test_background_approval_can_stop_before_action_and_cannot_replay(setup):
    agent, llm, _ = setup
    handler = AsyncMock(return_value={})
    agent.executor.registry.register(Tool("controlled", "test", Input, RiskLevel.CONFIRM, handler))
    llm.generate_response.return_value = LLMResponse(calls=[call()])
    result = await agent.message("request")
    token = result["confirmation"]["token"]
    await agent.lock.acquire()
    progress = agent.submit_confirmation(token, True)
    assert progress["request_id"] == result["request_id"]
    assert progress["result"] is None
    agent.stop_task(progress["request_id"])
    agent.lock.release()
    await agent.background
    assert agent.task_progress(progress["request_id"])["status"] == "cancelled"
    handler.assert_not_called()
    with pytest.raises(ValueError, match="already used"):
        agent.submit_confirmation(token, True)


async def test_busy_does_not_consume_approval_and_list_omits_results(setup):
    agent, llm, _ = setup
    handler = AsyncMock(return_value={})
    agent.executor.registry.register(Tool("controlled", "test", Input, RiskLevel.CONFIRM, handler))
    llm.generate_response.return_value = LLMResponse(calls=[call()])
    result = await agent.message("request")
    token = result["confirmation"]["token"]
    await agent.lock.acquire()
    other = agent.submit("another")
    with pytest.raises(ValueError, match="already active"):
        agent.submit_confirmation(token, True)
    agent.stop_task(other["request_id"])
    agent.lock.release()
    await agent.background
    agent.submit_confirmation(token, False)
    await agent.background
    assert agent.task_progress(result["request_id"])["status"] == "cancelled"
    assert all("result" not in item for item in agent.list_tasks())
    assert token not in str(agent.list_tasks())
    handler.assert_not_called()


async def test_stop_during_approved_action_skips_following_call(setup):
    agent, llm, _ = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def action(_):
        entered.set()
        await release.wait()
        return {"done": True}

    agent.executor.registry.register(Tool("controlled", "test", Input, RiskLevel.CONFIRM, action))
    llm.generate_response.return_value = LLMResponse(calls=[call(), call()])
    result = await agent.message("request")
    progress = agent.submit_confirmation(result["confirmation"]["token"], True)
    await asyncio.wait_for(entered.wait(), 2)
    assert agent.task_progress(progress["request_id"])["current_tool"] == "controlled"
    agent.stop_task(progress["request_id"])
    release.set()
    await agent.background
    final = agent.task_progress(progress["request_id"])
    assert final["status"] == "cancelled"
    assert final["completed_steps"] == 1


async def test_workflow_controls_cannot_race_queued_approval(setup):
    agent, llm, _ = setup
    handler = AsyncMock(return_value={})
    agent.executor.registry.register(Tool("controlled", "test", Input, RiskLevel.CONFIRM, handler))
    llm.generate_response.return_value = LLMResponse(calls=[call()])
    result = await agent.message("request")
    agent.submit_confirmation(result["confirmation"]["token"], True)
    # These calls acquire the uncontended lock before the scheduled task runs.
    with pytest.raises(ValueError, match="active task"):
        await agent.cancel_workflow(result["request_id"])
    with pytest.raises(ValueError, match="active task"):
        await agent.resume_workflow(result["request_id"])
    agent.stop_task(result["request_id"])
    await agent.background
    handler.assert_not_called()


def test_task_api_auth_and_polling(setup):
    agent, llm, settings = setup
    llm.generate_response.return_value = LLMResponse(text="done")
    with TestClient(create_app(settings, agent)) as client:
        assert client.post("/api/v1/tasks", json={"message": "hello"}).status_code == 401
        headers = {"Authorization": "Bearer test"}
        assert client.get("/api/v1/tasks").status_code == 401
        assert (
            client.post(
                "/api/v1/tasks/confirm", json={"token": "fake", "approved": True}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/v1/tasks/confirm", headers=headers, json={"token": "fake", "approved": True}
            ).status_code
            == 409
        )
        response = client.post("/api/v1/tasks", headers=headers, json={"message": "hello"})
        assert response.status_code == 202
        request_id = response.json()["request_id"]
        progress = client.get(f"/api/v1/tasks/{request_id}", headers=headers)
        assert progress.json()["result"]["message"] == "done"
        assert (
            client.get("/api/v1/tasks", headers=headers).json()["tasks"][0]["request_id"]
            == request_id
        )
        assert client.get("/api/v1/tasks/missing", headers=headers).status_code == 404
        assert client.post(f"/api/v1/tasks/{request_id}/cancel", headers=headers).status_code == 409
