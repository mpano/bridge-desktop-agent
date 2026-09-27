import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agent.context import AgentContext
from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm.models import LLMResponse, ToolCall
from app.main import local_command
from app.workflows.retention import PruneInput, RetentionPolicy, WorkflowQuery
from app.workflows.store import SQLiteWorkflows

OLD = "2000-01-01 00:00:00"


def seed(store, request_id, status="completed", date=OLD, in_flight=False):
    store.save(AgentContext(request_id=request_id), status, in_flight=in_flight)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE workflows SET updated_at=? WHERE request_id=?", (date, request_id))


def prune_args(store, keep=0):
    preview = store.preview_prune(RetentionPolicy(keep_recent=keep))
    return PruneInput(**{k: preview[k] for k in ("cutoff_utc", "keep_recent", "records")})


@pytest.fixture
def store(tmp_path):
    repository = SQLiteWorkflows(tmp_path / "db")
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def agent(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="test-token")
    result = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    yield result
    # All tests complete async work before teardown; no native/provider resources are opened.
    result.workflows.close()


def test_legacy_schema_migration_preserves_data(tmp_path):
    path = tmp_path / "db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE workflows (request_id TEXT PRIMARY KEY, status TEXT, "
            "snapshot TEXT, in_flight INTEGER, updated_at TEXT)"
        )
        db.execute(
            "INSERT INTO workflows VALUES (?, ?, ?, ?, ?)",
            ("old", "completed", '{"queue":[],"steps":[],"rounds":0}', 0, OLD),
        )
    store = SQLiteWorkflows(path)
    assert store.get("old")["status"] == "completed"
    assert prune_args(store).records[0].revision == 1
    store.close()


def test_page_filters_find_old_unfinished_records(store):
    seed(store, "pending", "confirmation_required")
    for index in range(110):
        seed(store, f"done-{index}")
    assert len(store.list()) == 100
    assert "pending" not in {row["request_id"] for row in store.list()}
    page = store.page(WorkflowQuery(status="unfinished"))
    assert page["total"] == 1
    assert page["workflows"][0]["request_id"] == "pending"
    first = store.page(WorkflowQuery(status="completed", limit=50))
    second = store.page(WorkflowQuery(status="completed", limit=50, offset=50))
    assert first["total"] == 110
    assert first["has_more"] and second["has_more"]
    assert not (
        {w["request_id"] for w in first["workflows"]}
        & {w["request_id"] for w in second["workflows"]}
    )
    assert not store.page(WorkflowQuery(offset=150))["has_more"]


def test_prune_only_reviewed_terminal_records(store):
    for status in (
        "completed",
        "failed",
        "cancelled",
        "paused",
        "interrupted",
        "running",
        "planning",
        "confirmation_required",
    ):
        seed(store, status, status)
    seed(store, "recent", date="2999-01-01 00:00:00")
    seed(store, "in-flight", in_flight=True)
    approved = prune_args(store)
    assert {r.request_id for r in approved.records} == {"completed", "failed", "cancelled"}
    seed(store, "newly-eligible")
    result = store.prune(approved)
    assert result["deleted_count"] == 3
    remaining = {row["request_id"] for row in store.list()}
    assert remaining == {
        "paused",
        "interrupted",
        "running",
        "planning",
        "confirmation_required",
        "recent",
        "in-flight",
        "newly-eligible",
    }


def test_keep_newest_and_batch_limit(store):
    for index in range(205):
        seed(store, f"id-{index}")
    preview = store.preview_prune(RetentionPolicy(keep_recent=2))
    assert preview["total_eligible"] == 203
    assert len(preview["records"]) == 200
    assert preview["has_more"]
    store.prune(prune_args(store, keep=2))
    assert store.page(WorkflowQuery())["total"] == 5
    assert store.get("id-204")["status"] == "completed"
    assert store.get("id-203")["status"] == "completed"


@pytest.mark.parametrize("mutation", ["revision", "status", "delete", "in_flight"])
def test_stale_preview_aborts_entire_transaction(store, mutation):
    seed(store, "a")
    seed(store, "b")
    arguments = prune_args(store)
    with sqlite3.connect(store.path) as db:
        if mutation == "revision":
            db.execute("UPDATE workflows SET revision=revision+1 WHERE request_id='b'")
        elif mutation == "status":
            db.execute("UPDATE workflows SET status='paused' WHERE request_id='b'")
        elif mutation == "delete":
            db.execute("DELETE FROM workflows WHERE request_id='b'")
        else:
            db.execute("UPDATE workflows SET in_flight=1 WHERE request_id='b'")
    with pytest.raises(ValueError, match="Nothing was removed"):
        store.prune(arguments)
    assert store.get("a")["status"] == "completed"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"older_than_days": 0},
        {"older_than_days": -1},
        {"older_than_days": 3651},
        {"keep_recent": -1},
        {"keep_recent": 10001},
        {"older_than_days": "30"},
        {"request_ids": ["anything"]},
    ],
)
def test_invalid_retention_policies(kwargs):
    with pytest.raises(ValidationError):
        RetentionPolicy(**kwargs)


async def test_confirmation_decline_replay_and_data_boundaries(agent, tmp_path):
    seed(agent.workflows, "old")
    project_file = tmp_path / "project.txt"
    project_file.write_text("keep me")
    result = await agent.preview_workflow_cleanup(RetentionPolicy(keep_recent=0))
    assert result["status"] == "confirmation_required"
    assert result["confirmation"]["arguments"]["records"][0]["request_id"] == "old"
    assert agent.workflows.get("old")["status"] == "completed"
    await agent.confirm(result["confirmation"]["token"], False)
    assert agent.workflows.get("old")["status"] == "completed"
    result = await agent.preview_workflow_cleanup(RetentionPolicy(keep_recent=0))
    token = result["confirmation"]["token"]
    result = await agent.confirm(token, True)
    assert result["status"] == "completed"
    assert result["steps"][0]["result"]["deleted_count"] == 1
    with pytest.raises(ValueError):
        agent.workflows.get("old")
    with pytest.raises(ValueError):
        await agent.confirm(token, True)
    assert project_file.read_text() == "keep me"
    agent.planner.llm.generate_response.assert_not_called()


async def test_private_tool_hidden_and_model_cannot_guess_it(agent):
    definitions = agent.executor.registry.definitions()
    assert "prune_workflow_records" not in {d["name"] for d in definitions}
    seed(agent.workflows, "old")
    arguments = prune_args(agent.workflows).model_dump()
    agent.planner.llm.generate_response.return_value = LLMResponse(
        [ToolCall(call_id="guess", name="prune_workflow_records", arguments=arguments)]
    )
    response = await agent.message("Do something")
    assert response["status"] == "failed"
    assert "confirmation" not in response
    assert agent.workflows.get("old")["status"] == "completed"


async def test_cleanup_cannot_resume_after_restart(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db")
    first = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    seed(first.workflows, "old")
    result = await first.preview_workflow_cleanup(RetentionPolicy(keep_recent=0))
    snapshot = first.get_workflow(result["request_id"])
    assert not snapshot["recoverable"]
    assert snapshot["queue"][0]["arguments_omitted"]
    await first.close()
    second = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    try:
        with pytest.raises(ValueError):
            await second.confirm(result["confirmation"]["token"], True)
        with pytest.raises(ValueError):
            await second.resume_workflow(result["request_id"])
        assert second.get_workflow("old")["status"] == "completed"
    finally:
        await second.close()


async def test_cli_filters_and_cleanup_use_service(agent, capsys):
    seed(agent.workflows, "old")
    seed(agent.workflows, "unfinished", "paused")
    await local_command(agent, "/workflows unfinished 1")
    page = json.loads(capsys.readouterr().out)
    assert page["total"] == 1
    result = await local_command(agent, "/prune-workflows 30 0")
    assert result["status"] == "confirmation_required"
    assert agent.get_workflow("old")["status"] == "completed"


def test_cleanup_api_requires_auth_and_validates_query(agent):
    settings = Settings(_env_file=None, api_token="test-token")
    seed(agent.workflows, "old")
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(create_app(settings, agent)) as client:
        url = "/api/v1/workflows/cleanup/preview"
        assert client.post(url, json={}).status_code == 401
        assert (
            client.post(url, headers={**headers, "Origin": "https://evil"}, json={}).status_code
            == 403
        )
        assert client.post(url, headers=headers, json={"older_than_days": 0}).status_code == 422
        for query in ("status=invalid", "offset=-1", "limit=101"):
            assert client.get("/api/v1/workflows?" + query, headers=headers).status_code == 422
        data = client.get("/api/v1/workflows?status=completed&limit=1", headers=headers).json()
        assert data["total"] == 1
        assert data["limit"] == 1
        result = client.post(url, headers=headers, json={"keep_recent": 0}).json()
        assert result["status"] == "confirmation_required"
        result = client.post(
            "/api/v1/agent/confirm",
            headers=headers,
            json={"token": result["confirmation"]["token"], "approved": True},
        ).json()
        assert result["status"] == "completed"
