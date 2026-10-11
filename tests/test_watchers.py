"""Watchers: pull requests, releases, Jira issues, apps and "only once" email. All mocked."""

import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.agent.proactive import Proactive
from app.assistant.watchers import Watchers
from app.integrations.work import GitHub, Jira
from app.security.privacy import ToolResultPrivacy
from app.tools.system.proactive import ProactiveController, WatchInput, render_created
from app.workflows.schedules import ScheduleStore
from app.workflows.watches import ProactiveStore


class Services:
    """GitHub and Jira as Bridge's token connections see them: answers by path."""

    def __init__(self):
        self.pr = {
            "state": "open",
            "merged": False,
            "title": "Faster sync",
            "comments": 0,
            "review_comments": 0,
            "head": {"sha": "abc"},
            "html_url": "https://gh/pr/412",
        }
        self.reviews: list = []
        self.runs: list = []
        self.release = None
        self.issue = {"status": "In Progress", "done": False, "comments": []}
        self.calls: list[str] = []

    async def accounts(self):
        return {"github": {}, "jira": {}}

    async def site(self):
        return "team.atlassian.net"

    async def request(self, provider, method, path, **kwargs):
        self.calls.append(path)
        if path == "search/issues":
            return {
                "items": [
                    {"number": 412, "repository_url": "https://api.github.com/repos/acme/app"},
                    {"number": 7, "repository_url": "https://api.github.com/repos/acme/web"},
                ]
            }
        if path == "repos/acme/app/pulls/412":
            return dict(self.pr)
        if path == "repos/acme/app/pulls/412/reviews":
            return list(self.reviews)
        if path.startswith("repos/acme/app/commits/"):
            return {"check_runs": list(self.runs)}
        if path == "repos/acme/app/releases/latest":
            if self.release is None:
                raise RuntimeError("Not found, or you can't see it.")
            return {"tag_name": self.release, "name": f"Release {self.release}", "html_url": "u"}
        if path.startswith("issue/ABC-9"):
            fields = {
                "summary": "Login bug",
                "status": {
                    "name": self.issue["status"],
                    "statusCategory": {"key": "done" if self.issue["done"] else "x"},
                },
                "comment": {"comments": self.issue["comments"]},
            }
            return {"key": "ABC-9", "fields": fields}
        raise AssertionError(f"Unexpected request {path}")


def setup(tmp_path, llm=None, mcp=None, privacy=None):
    services = Services()
    store = ProactiveStore(tmp_path / "db")
    notify = AsyncMock()
    watchers = Watchers(llm, github=GitHub(services), jira=Jira(services), mcp=mcp, privacy=privacy)
    engine = Proactive(store, notify)
    engine.watchers = watchers
    controller = ProactiveController(store, ScheduleStore(tmp_path / "db"))
    controller.watchers, controller.proactive = watchers, engine
    return services, store, notify, engine, controller


async def test_pr_merged_finds_the_repo_and_tells_you_once(tmp_path):
    services, store, notify, engine, controller = setup(tmp_path)
    made = await controller.watch(
        WatchInput(kind="github_pr", number=412, when="merged", label="PR 412 merged")
    )
    assert made["status"] == "active" and made["once"] is True
    assert made["query"] == "acme/app#412 is merged"
    assert await engine.check_watches() == 0
    services.pr.update(merged=True, state="closed", merged_by={"login": "dana"})
    assert await engine.check_watches() == 1
    title, body = notify.await_args.args
    assert title == "✓ PR #412 was merged by dana" and body == "Faster sync · acme/app"
    assert store.watches() == []  # Done: no more checks.
    finished = store.watches(everything=True)[0]
    assert finished.status == "done" and "merged" in finished.fired
    assert await engine.check_watches() == 0 and notify.await_count == 1


async def test_already_happened_is_said_right_away_without_a_notification(tmp_path):
    services, store, notify, engine, controller = setup(tmp_path)
    services.reviews = [{"user": {"login": "lee"}, "state": "APPROVED"}]
    made = await controller.watch(
        WatchInput(kind="github_pr", repo="acme/app", number=412, when="approved", label="PR ok")
    )
    assert made["status"] == "done"
    assert render_created(made).startswith("It already happened: ✓ PR #412 was approved")
    notify.assert_not_awaited()


async def test_checks_and_comments_need_a_change(tmp_path):
    services, store, notify, engine, controller = setup(tmp_path)
    services.runs = [{"status": "in_progress", "conclusion": None}]
    await controller.watch(
        WatchInput(kind="github_pr", repo="acme/app", number=412, when="checks_done", label="CI")
    )
    await controller.watch(
        WatchInput(kind="github_pr", repo="acme/app", number=412, when="comment", label="Talk")
    )
    assert await engine.check_watches() == 0
    services.runs = [{"status": "completed", "conclusion": "failure"}]
    services.pr["comments"] = 2
    assert await engine.check_watches() == 2
    titles = [call.args[0] for call in notify.await_args_list]
    assert titles == ["PR #412: checks failed", "PR #412: new comment"]
    # "comment" keeps watching (every time); "checks_done" was a one-off.
    assert [w.label for w in store.watches()] == ["Talk"]


async def test_jira_status_and_comments(tmp_path):
    services, store, notify, engine, controller = setup(tmp_path)
    made = await controller.watch(
        WatchInput(kind="jira", key="abc-9", status="QA", label="ABC-9 in QA")
    )
    assert made["query"] == "ABC-9 moves to QA" and made["status"] == "active"
    await controller.watch(WatchInput(kind="jira", key="ABC-9", when="comment", label="Notes"))
    services.issue["status"] = "Ready for QA"
    services.issue["comments"] = [{"author": {"displayName": "Ana"}, "created": "t1"}]
    assert await engine.check_watches() == 2
    titles = [call.args[0] for call in notify.await_args_list]
    assert titles == ["ABC-9 moved to Ready for QA", "ABC-9: new comment from Ana"]


async def test_new_releases_every_time(tmp_path):
    services, store, notify, engine, controller = setup(tmp_path)
    await controller.watch(WatchInput(kind="github_release", repo="acme/app", label="Releases"))
    services.release = "v2.0"
    assert await engine.check_watches() == 1
    assert notify.await_args.args[0] == "acme/app released v2.0"
    assert await engine.check_watches() == 0
    services.release = "v2.1"
    assert await engine.check_watches() == 1
    assert store.watches()[0].status == "active"


async def test_a_watch_that_cant_be_read_isnt_kept(tmp_path):
    services, store, notify, engine, controller = setup(tmp_path)
    with pytest.raises(ValueError, match="Couldn't read acme/app#999"):
        await controller.watch(
            WatchInput(kind="github_pr", repo="acme/app", number=999, when="merged", label="x")
        )
    assert store.watches(everything=True) == []
    with pytest.raises(ValueError, match="several repos|couldn't find"):
        await controller.watch(WatchInput(kind="github_pr", number=55, label="y"))


class Apps:
    def __init__(self, read_only=True):
        self.text = "3 open bugs"
        self.read_only = read_only
        self.calls = 0

    def find(self, name):
        return {"id": "lin1", "name": "Linear"}

    def server(self, server_id):
        return {"id": "lin1", "name": "Linear"}

    async def ensure(self, server):
        return None

    def tool_info(self, server, tool):
        return {"name": tool, "read_only": self.read_only}

    def asks_first(self, server, tool):
        return not tool["read_only"]

    def mode(self, server, tool):
        return "auto"

    async def call(self, server, tool, arguments):
        self.calls += 1
        return {"text": self.text, "is_error": False}


async def test_app_watchers_only_read_and_only_ask_the_model_when_shared(tmp_path):
    llm = AsyncMock()
    llm.complete.return_value = '{"met": true, "summary": "Bug LIN-4 was closed"}'
    apps = Apps()
    privacy = ToolResultPrivacy("allowlist", set())
    services, store, notify, engine, controller = setup(tmp_path, llm, apps, privacy)
    args = dict(
        kind="app",
        app="linear",
        tool="list_issues",
        arguments={"team": "core"},
        condition="LIN-4 is closed",
        label="LIN-4 closed",
    )
    await controller.watch(WatchInput(**args))
    llm.complete.assert_not_awaited()  # Not shared with OpenAI: no model call at all.
    store.observed(store.watches()[0].id, store.watches()[0].state)
    with sqlite3.connect(tmp_path / "db") as db:
        db.execute("UPDATE watches SET last_checked = 0")
    apps.text = "2 open bugs"
    assert await engine.check_watches() == 1
    assert notify.await_args.args[0] == "Linear changed"
    llm.complete.assert_not_awaited()

    privacy.allow({"mcp:lin1"})
    with sqlite3.connect(tmp_path / "db") as db:
        db.execute("UPDATE watches SET last_checked = 0")
    apps.text = "1 open bug"
    assert await engine.check_watches() == 1
    assert notify.await_args.args == ("LIN-4 closed", "Linear: Bug LIN-4 was closed")
    assert "untrusted" in llm.complete.await_args.args[0]
    assert engine.store.watches(everything=True)[0].state == {
        "hash": engine.store.watches(everything=True)[0].state["hash"]
    }  # Only a fingerprint of the app's data is kept.

    with pytest.raises(ValueError, match="only use tools that just read"):
        controller.watchers.mcp = Apps(read_only=False)
        await controller.watch(WatchInput(**{**args, "tool": "close_issue"}))


async def test_email_once_stops_after_the_reply(tmp_path):
    store = ProactiveStore(tmp_path / "db")
    notify = AsyncMock()
    gmail = AsyncMock()
    gmail.search.return_value = {"messages": []}
    engine = Proactive(store, notify, gmail=gmail)
    store.add_watch("email", "from:sarah", "Sarah replies", once=True)
    await engine.check_watches()
    gmail.search.return_value = {
        "messages": [
            {"message_id": "m2", "from": "Sarah <s@x.com>", "subject": "Re: budget"},
            {"message_id": "m1", "from": "Sarah <s@x.com>", "subject": "Re: plan"},
        ]
    }
    assert await engine.check_watches() == 1
    assert notify.await_args.args[0] == "✓ Sarah replies"
    assert store.watches() == [] and store.watches(everything=True)[0].status == "done"


async def test_waiting_watches_give_up(tmp_path):
    store = ProactiveStore(tmp_path / "db")
    notify = AsyncMock()
    engine = Proactive(store, notify)
    watch = store.add_watch("jira", "ABC-9 is done", "Done", target={"key": "ABC-9"}, once=True)
    assert watch.expires_at > time.time() + 13 * 86400
    with sqlite3.connect(tmp_path / "db") as db:
        db.execute("UPDATE watches SET expires_at = ?", (time.time() - 1,))
    await engine.check_watches()
    assert notify.await_args.args[0] == "Stopped watching: Done"
    assert store.watches(everything=True)[0].status == "expired"


def test_old_watch_tables_are_upgraded(tmp_path):
    with sqlite3.connect(tmp_path / "db") as db:
        db.execute(
            "CREATE TABLE watches (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
            "query TEXT NOT NULL, label TEXT NOT NULL, seen TEXT NOT NULL DEFAULT '[]', "
            "baseline_done INTEGER NOT NULL DEFAULT 0, last_checked REAL, last_error TEXT, "
            "created_at REAL NOT NULL)"
        )
        db.execute(
            "INSERT INTO watches (kind, query, label, created_at) "
            "VALUES ('email', 'from:o', 'O', 1)"
        )
    store = ProactiveStore(tmp_path / "db")
    [old] = store.watches()
    assert old.status == "active" and old.target == {} and old.once is False


def test_tell_me_when_in_plain_words(tmp_path, monkeypatch):
    from app.api.server import create_app
    from app.bootstrap import build_agent
    from app.config.settings import Settings

    monkeypatch.chdir(tmp_path)
    config = Settings(
        _env_file=None, database_path=tmp_path / "db", api_token="token", openai_api_key="sk-x"
    )
    agent = build_agent(config, llm=AsyncMock())
    services = Services()
    agent.watchers.github, agent.watchers.jira = GitHub(services), Jira(services)
    agent.watchers.llm = SimpleNamespace(complete=AsyncMock())
    agent.watchers.llm.complete.return_value = json.dumps(
        {
            "kind": "github_pr",
            "number": "412",
            "when": "merged",
            "label": "PR 412 merged",
            "extra": "ignored",
        }
    )
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(config, agent, enable_ui=True)) as client:
        made = client.post(
            "/api/v1/watches/ask", json={"text": "tell me when PR 412 is merged"}, headers=headers
        )
        assert made.status_code == 200, made.text
        assert made.json()["label"] == "PR 412 merged"
        listed = client.get("/api/v1/automations", headers=headers).json()["watches"]
        assert listed[0]["query"] == "acme/app#412 is merged" and listed[0]["url"]
        agent.watchers.llm.complete.return_value = '{"error": "I can\'t watch the weather."}'
        refused = client.post(
            "/api/v1/watches/ask", json={"text": "when it rains"}, headers=headers
        )
        assert refused.status_code == 422 and "weather" in refused.json()["detail"]
        agent.watchers.llm.complete.return_value = '{"kind": "jira", "label": "x", "key": "nope"}'
        bad = client.post("/api/v1/watches/ask", json={"text": "jira thing"}, headers=headers)
        assert bad.status_code == 422 and "ABC-9" in bad.json()["detail"]
        assert client.post("/api/v1/watches/ask", json={"text": "x" * 5}).status_code == 401
