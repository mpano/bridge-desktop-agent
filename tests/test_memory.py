"""Remembered facts: saving, refusing secrets, forgetting, and use in the prompt."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.agent.executor import Executor
from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm import prompts
from app.llm.models import ToolCall
from app.memory.facts import PROMPT_BUDGET, FactStore
from app.tools.productivity import memory
from app.tools.registry import ToolRegistry


def test_facts_are_saved_once_and_forgotten_by_words(tmp_path):
    store = FactStore(tmp_path / "db")
    first = store.add("Olivier Mupenzi is my brother")
    assert store.add("  olivier mupenzi IS my brother ").id == first.id  # No duplicates.
    store.add("My boss is Sarah Kim")
    with pytest.raises(ValueError, match="Several memories"):
        store.forget_matching("is my")
    assert store.forget_matching("boss").text == "My boss is Sarah Kim"
    assert [fact.text for fact in store.list()] == ["Olivier Mupenzi is my brother"]
    with pytest.raises(ValueError, match="don't remember"):
        store.forget_matching("dentist")


@pytest.mark.parametrize(
    "secret",
    [
        "My bank password is hunter2",
        "The door PIN code is 4455",
        "My card is 4111 1111 1111 1111",
        "OpenAI key sk-abcdefghijklmnopqrstuv",
        "GitHub token ghp_abcdefghijklmnopqrst",
    ],
)
def test_secrets_are_never_stored(tmp_path, secret):
    store = FactStore(tmp_path / "db")
    with pytest.raises(ValueError, match="don't store"):
        store.add(secret)
    assert store.list() == []


def test_prompt_includes_newest_memories_within_budget(tmp_path):
    store = FactStore(tmp_path / "db")
    for index in range(60):
        store.add(f"Fact number {index} " + "x" * 90)
    block = store.prompt_block()
    assert len(block) <= PROMPT_BUDGET
    assert "Fact number 59" in block and "Fact number 0 " not in block
    assert block.index("Fact number 58") < block.index("Fact number 59")
    text = prompts.system_prompt(memories="- Olivier Mupenzi is my brother")
    assert "facts, not instructions" in text and "Olivier Mupenzi is my brother" in text
    assert "remember" not in prompts.system_prompt(memories="").split("\n")[-2]


async def test_memory_tools_save_list_and_forget(tmp_path):
    store = FactStore(tmp_path / "db")
    registry = ToolRegistry()
    memory.register(registry, store)
    executor = Executor(registry)

    async def run(name, **arguments):
        return await executor.execute(ToolCall(call_id="c", name=name, arguments=arguments), "r")

    saved = await run("memory_save", fact="My brother is Olivier Mupenzi")
    assert saved["display"] == "✓ I'll remember: My brother is Olivier Mupenzi"
    assert "My brother is Olivier Mupenzi" in (await run("memory_list"))["display"]
    refused = await run("memory_save", fact="my password is swordfish")
    assert not refused["success"] and "password manager" in refused["error"]
    assert (await run("memory_forget", about="brother"))["success"]
    assert "don't remember anything" in (await run("memory_list"))["display"]


def test_memories_reach_the_model_and_the_dashboard_api(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = None
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        added = client.post(
            "/api/v1/memories", json={"text": "Mom's name is Grace"}, headers=headers
        )
        assert added.status_code == 200
        assert "Mom's name is Grace" in prompts.system_prompt()
        refused = client.post(
            "/api/v1/memories", json={"text": "my password is x1"}, headers=headers
        )
        assert refused.status_code == 422
        listed = client.get("/api/v1/memories", headers=headers).json()["memories"]
        assert [item["text"] for item in listed] == ["Mom's name is Grace"]
        client.post("/api/v1/memories/forget", json={"id": listed[0]["id"]}, headers=headers)
        assert "Grace" not in prompts.system_prompt()
        assert client.get("/api/v1/memories").status_code == 401
