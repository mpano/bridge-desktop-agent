"""Settings › Health: each part of Bridge, whether it works, and its fix."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings


def test_health_lists_every_part_with_a_fix(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = agent.proactive = None
    agent.operator.hands = None
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        data = client.get("/api/v1/health", headers=headers).json()
        checks = {c["id"]: c for c in data["checks"]}
        # No key: broken, with the way to fix it (and no request left the Mac to find out).
        assert checks["openai"]["state"] == "error"
        assert checks["openai"]["fix"] == {
            "label": "Add a key",
            "view": "settings",
            "section": "privacy",
        }
        assert data["errors"] >= 1
        for key in ("gmail", "slack", "jira", "github", "phone", "talk", "operator"):
            assert checks[key]["state"] == "off" and checks[key]["fix"], key
        assert checks["operator"]["fix"]["section"] == "operator"
        assert checks["chats"]["state"] == "ok"
        assert {c["area"] for c in data["checks"]} >= {
            "Basics",
            "Connections",
            "Assistant",
            "Phone & voice",
        }
        assert client.get("/api/v1/health").status_code == 401


def test_health_covers_automations_and_the_mac_features(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = agent.proactive = None
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        checks = {
            c["id"]: c for c in client.get("/api/v1/health", headers=headers).json()["checks"]
        }
        assert (
            checks["watchers"]["state"] == "off"
            and checks["watchers"]["fix"]["view"] == "automations"
        )
        assert checks["recipes"]["state"] == "off"
        assert (
            checks["report"]["state"] == "ok" and "Fridays at 16:00" in checks["report"]["detail"]
        )
        assert checks["guide"]["state"] == "off" and checks["guide"]["fix"]["section"] == "operator"
        # What's this? needs the screen-sharing OK first.
        assert (
            checks["explain"]["state"] == "warn"
            and checks["explain"]["fix"]["section"] == "privacy"
        )
        assert checks["desk"]["state"] == "ok"

        store = agent.proactive_store
        broken = store.add_watch("jira", "ABC-9 is done", "ABC-9 done", target={"key": "ABC-9"})
        store.failed(broken.id, "Connect Jira in Connections first.")
        settings.operator_enabled = True
        settings.desk_buddy = False
        checks = {
            c["id"]: c for c in client.get("/api/v1/health", headers=headers).json()["checks"]
        }
        assert (
            checks["watchers"]["state"] == "warn" and "Connect Jira" in checks["watchers"]["detail"]
        )
        assert checks["guide"]["state"] == "ok" and checks["explain"]["state"] == "ok"
        assert checks["desk"]["state"] == "off"
        areas = {c["area"] for c in checks.values()}
        assert {"Automations", "On your Mac"} <= areas


def test_failures_are_said_plainly_with_the_fix():
    import time

    from app.api.health import failing

    now = time.time()
    expired = failing("slack", "Slack", {"at": now, "detail": "Slack said: invalid_auth"})
    assert expired["state"] == "error" and expired["detail"].startswith("Login expired")
    assert expired["fix"] == {"label": "Reconnect", "view": "connections"}
    scope = failing("gmail", "Gmail", {"at": now, "detail": "HTTP 403 Forbidden"})
    assert "more permission" in scope["detail"] and scope["state"] == "error"
    slow = failing("github", "GitHub", {"at": now, "detail": "429 Too Many Requests"})
    assert slow["state"] == "warn" and slow["fix"] is None
    offline = failing("jira", "Jira", {"at": now, "detail": "Request timed out"})
    assert offline["state"] == "warn" and "tries again" in offline["detail"]
    other = failing("slack", "Slack", {"at": now, "event": "Posted", "detail": "channel_not_found"})
    assert other["state"] == "error" and "channel_not_found" in other["detail"]


def test_ai_spending_this_week(tmp_path):
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from app.api.health import spending
    from app.llm.usage import Usage

    day = datetime(2026, 10, 14, 12)
    settings = SimpleNamespace(openai_model="gpt-5-mini", monthly_budget=0)
    usage = Usage(tmp_path / "db", settings, clock=lambda: day.timestamp())
    with usage._db() as db:
        for when, task, cost in [
            (day, "ask", 2.0),
            (day - timedelta(days=6), "inbox", 0.5),
            (day - timedelta(days=8), "ask", 1.0),  # The week before.
            (day - timedelta(days=20), "ask", 9.0),  # Older: not counted.
        ]:
            db.execute(
                "INSERT INTO llm_usage (day, task, model, calls, input, cached, output, cost, "
                "priced) VALUES (?, ?, 'gpt-5-mini', 3, 1, 0, 1, ?, 1)",
                (when.strftime("%Y-%m-%d"), task, cost),
            )
    week = usage.week()
    assert (week["spent"], week["previous"], week["calls"]) == (2.5, 1.0, 6)
    assert [t["task"] for t in week["top"]] == ["ask", "inbox"]
    jumped = spending(week)
    assert jumped["state"] == "warn" and "More than double" in jumped["detail"]
    assert "Ask and voice commands" in jumped["detail"]
    settings.monthly_budget = 3
    near = spending(usage.week())
    assert near["state"] == "warn" and "Over this month's $3.00 budget" in near["detail"]
    settings.monthly_budget = 100
    calm = spending({**usage.week(), "previous": 2.0})
    assert calm["state"] == "ok" and calm["detail"].startswith("$2.50 in the last 7 days")


async def test_a_broken_connection_is_told_once():
    from app.api.health import Alerts, watch

    alerts = Alerts()
    slack = {"id": "slack", "area": "Connections", "name": "Slack", "state": "error", "detail": "x"}
    denied = {
        "id": "mic",
        "area": "Mac permissions",
        "name": "Mic",
        "state": "error",
        "detail": "y",
    }
    nokey = {
        "id": "openai",
        "area": "Basics",
        "name": "OpenAI key",
        "state": "error",
        "detail": "No OpenAI key yet.",
    }
    assert alerts.news([slack, denied, nokey]) == [slack]  # Only what broke on its own.
    assert alerts.news([slack]) == []
    assert alerts.news([{**slack, "state": "ok"}]) == []
    assert alerts.news([slack]) == [slack]  # Broken again later: told again.

    told = []

    async def notify(title, message, view):
        told.append((title, message, view))
        raise asyncio.CancelledError  # Stop the loop after the first look.

    async def collect(agent):
        return {"checks": [slack]}

    import asyncio

    agent = SimpleNamespace(notify=notify)
    try:
        await watch(collect, agent, first=0)
    except asyncio.CancelledError:
        pass
    assert told == [("Slack needs you", "x", "health")]


def test_health_shows_the_week_and_moments(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = agent.proactive = None
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        data = client.get("/api/v1/health", headers=headers).json()
        assert data["week"]["spent"] == 0 and data["week"]["top"] == []
        checks = {c["id"]: c for c in data["checks"]}
        assert checks["spending"]["state"] == "ok"
        assert checks["moments"]["area"] == "On your Mac"
        assert callable(client.app.state.health_watch)
