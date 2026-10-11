"""AI spending and model routing: costs, budgets, Smart vs Fast."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm.client import OpenAILLMClient
from app.llm.usage import Usage, cost, task

DAY = 1_791_000_000.0  # 2026-10-03


def settings(**extra):
    base = {
        "openai_model": "gpt-4.1-mini",
        "model_smart": "gpt-6.1-sol",
        "model_fast": "gpt-6-luna",
        "monthly_budget": 0.0,
        "budget_mode": "warn",
        "openai_api_key": SecretStr("sk-test"),
    }
    return SimpleNamespace(**{**base, **extra})


def test_costs_follow_openai_prices():
    assert cost("gpt-4.1-mini", 1_000_000, 0, 1_000_000) == 0.40 + 1.60
    assert (
        cost("gpt-4.1-mini", 1_000_000, 500_000, 0) == 0.5 * 0.40 + 0.5 * 0.10
    )  # Cached is cheaper.
    assert cost("gpt-4.1-mini-2025-04-14", 1_000_000, 0, 0) == 0.40  # Dated snapshot.
    assert cost("some-new-model", 10, 0, 10) is None


def test_record_summarize_and_route(tmp_path):
    meter = Usage(tmp_path / "db", settings(), clock=lambda: DAY)
    meter.record("inbox", "gpt-6-luna", 2_000_000, 0, 100_000)
    meter.record("ask", "gpt-6.1-sol", 1_000_000, 0, 100_000)
    meter.record("ask", "gpt-6.1-sol", 1_000_000, 0, 100_000)
    data = meter.summary()
    assert [t["task"] for t in data["tasks"]] == ["ask", "inbox"]
    assert data["tasks"][0]["calls"] == 2 and round(data["tasks"][0]["cost"], 4) == 6.0
    assert round(data["spent"], 2) == 6.25 and data["projected"] > data["spent"]
    assert meter.model_for("inbox") == "gpt-6-luna" and meter.model_for("ask") == "gpt-6.1-sol"
    assert (
        meter.model_for("drafts") == "gpt-6.1-sol" and meter.model_for("promises") == "gpt-6-luna"
    )


def test_budget_alerts_once_and_save_money_mode(tmp_path):
    notes = []

    async def notify(title, body, view):
        notes.append(title)

    meter = Usage(
        tmp_path / "db", settings(monthly_budget=10.0, budget_mode="save"), clock=lambda: DAY
    )
    meter.notify = notify
    meter.record("ask", "gpt-6.1-sol", 4_000_000, 0, 0)  # $8.
    asyncio.run(meter.check_budget())
    asyncio.run(meter.check_budget())
    assert notes == ["AI spending: 80% of your $10 budget"]
    assert meter.model_for("ask") == "gpt-6.1-sol" and meter.allow_background("promises")
    meter.record("ask", "gpt-6.1-sol", 1_000_000, 0, 0)  # $10: over.
    asyncio.run(meter.check_budget())
    assert notes[-1] == "AI spending: 100% of your $10 budget"
    assert meter.model_for("ask") == "gpt-6-luna"  # Everything on the fast model.
    assert not meter.allow_background("promises") and meter.allow_background("ask")
    meter.settings.budget_mode = "warn"
    assert meter.model_for("ask") == "gpt-6.1-sol"  # Warn only: nothing changes.


def test_every_model_call_is_routed_and_counted(tmp_path):
    meter = Usage(tmp_path / "db", settings(), clock=lambda: DAY)
    client = OpenAILLMClient(settings())
    client.usage = meter
    seen = []

    async def create(**kwargs):
        seen.append(kwargs["model"])
        return SimpleNamespace(
            output_text="ok",
            output=[],
            usage=SimpleNamespace(
                input_tokens=1000,
                output_tokens=200,
                input_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )

    client._client = SimpleNamespace(responses=SimpleNamespace(create=create))

    async def scenario():
        await client.complete("x", "y")  # Ask by default.
        with task("inbox"):
            await client.complete("x", "y")

    asyncio.run(scenario())
    assert seen == ["gpt-6.1-sol", "gpt-6-luna"]
    tasks = {t["task"]: t["calls"] for t in meter.summary()["tasks"]}
    assert tasks == {"ask": 1, "inbox": 1}


def test_usage_page_and_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    agent = build_agent(config, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = agent.proactive = None
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(config, agent, enable_ui=True)) as client:
        data = client.get("/api/v1/usage", headers=headers).json()
        assert data["spent"] == 0 and data["smart"] == data["fast"] == "gpt-4.1-mini"
        assert {r["task"]: r["tier"] for r in data["routing"]}["inbox"] == "fast"
        bad = client.post("/api/v1/usage/settings", json={"smart": "made-up"}, headers=headers)
        assert bad.status_code == 422
        saved = client.post(
            "/api/v1/usage/settings", json={"budget": 25, "mode": "save"}, headers=headers
        ).json()
        assert saved["budget"] == 25 and saved["budget_mode"] == "save"
        assert "MONTHLY_BUDGET=25" in (tmp_path / ".env").read_text()
        agent.talk.sessions["s" * 16] = {
            "started": 0,
            "from_phone": False,
            "pending": None,
            "ended": False,
        }
        agent.talk.end("s" * 16, 60, {"input": 6000, "cached": 0, "output": 12000})
        talk = [
            t
            for t in client.get("/api/v1/usage", headers=headers).json()["tasks"]
            if t["task"] == "talk"
        ]
        assert talk and round(talk[0]["cost"], 3) == round((6000 * 32 + 12000 * 64) / 1e6, 3)
