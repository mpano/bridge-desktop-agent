"""Jira, Confluence and GitHub connected with a token (or the gh command line)."""

import asyncio
import base64
import json
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.assistant.briefs import Briefs
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.integrations import tokens as tokens_module
from app.integrations.models import IntegrationError
from app.integrations.tokens import MemoryTokens, TokenConnections
from app.integrations.work import GitHub, Jira, adf, adf_text
from app.security.risk import RiskLevel
from app.workflows.watches import ProactiveStore

SITE = "acme.atlassian.net"


class Fake:
    """A pretend Jira + GitHub that records every request."""

    def __init__(self):
        self.seen: list[httpx.Request] = []
        self.transitions_done = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        url, path = str(request.url), request.url.path
        if request.url.host == SITE:
            if path == "/rest/api/3/myself":
                return httpx.Response(
                    200, json={"accountId": "acc-1", "emailAddress": "me@acme.test"}
                )
            if path == "/rest/api/3/search/jql":
                return httpx.Response(
                    200,
                    json={
                        "issues": [
                            {
                                "key": "APP-7",
                                "fields": {
                                    "summary": "Fix login",
                                    "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                                    "priority": {"name": "High"},
                                    "issuetype": {"name": "Bug"},
                                    "project": {"key": "APP"},
                                    "duedate": "2026-10-05",
                                },
                            }
                        ]
                    },
                )
            if path == "/rest/api/3/issue" and request.method == "POST":
                return httpx.Response(201, json={"key": "APP-8"})
            if path == "/rest/api/3/issue/APP-7/transitions" and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "transitions": [
                            {"id": "21", "name": "Start", "to": {"name": "In Progress"}},
                            {"id": "31", "name": "Finish", "to": {"name": "Done"}},
                        ]
                    },
                )
            if path == "/rest/api/3/issue/APP-7/transitions":
                self.transitions_done.append(json.loads(request.content))
                return httpx.Response(204)
            if path == "/rest/api/3/issue/APP-7/comment":
                return httpx.Response(201, json={"id": "c1"})
            if path == "/wiki/rest/api/search":
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "content": {"id": "123", "title": "Onboarding"},
                                "excerpt": "<b>Steps</b> to start",
                                "resultGlobalContainer": {"title": "Eng"},
                                "url": "/spaces/ENG/pages/123",
                            }
                        ]
                    },
                )
        if request.url.host == "api.github.com":
            if path == "/user":
                return httpx.Response(200, json={"login": "mpano"})
            if path == "/search/issues":
                q = request.url.params["q"]
                author = "ana" if "review-requested" in q else "mpano"
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "repository_url": "https://api.github.com/repos/acme/web",
                                "number": 42,
                                "title": "Add pricing page",
                                "user": {"login": author},
                                "html_url": "https://github.com/acme/web/pull/42",
                            }
                        ]
                    },
                )
            if path == "/repos/acme/web/pulls/42":
                return httpx.Response(
                    200, json={"head": {"sha": "abc"}, "mergeable_state": "blocked"}
                )
            if path == "/repos/acme/web/commits/abc/check-runs":
                return httpx.Response(
                    200, json={"check_runs": [{"conclusion": "success"}, {"conclusion": "failure"}]}
                )
            if path == "/repos/acme/web/issues" and request.method == "POST":
                return httpx.Response(
                    201, json={"number": 43, "html_url": "https://github.com/acme/web/issues/43"}
                )
        return httpx.Response(404, json={"message": f"no fake for {url}"})


def connections(fake):
    return TokenConnections(
        MemoryTokens(), http=httpx.AsyncClient(transport=httpx.MockTransport(fake))
    )


def test_jira_connects_with_a_token_to_your_atlassian_site_only():
    fake = Fake()
    c = connections(fake)
    with pytest.raises(IntegrationError, match="atlassian.net"):
        asyncio.run(c.connect("jira", site="evil.example.com", email="me@acme.test", token="t"))
    result = asyncio.run(
        c.connect("jira", site=f"https://{SITE}/", email="me@acme.test", token="secret-token")
    )
    assert result == {"connected": True, "identity": "me@acme.test"}
    auth = fake.seen[-1].headers["Authorization"]
    assert base64.b64decode(auth.split()[1]).decode() == "me@acme.test:secret-token"
    listed = asyncio.run(c.catalog())
    assert "secret-token" not in json.dumps(listed)
    jira = next(item for item in listed if item["provider"] == "jira")
    assert jira["account"]["site"] == SITE and jira["account"]["actions"] is True


def test_jira_issues_create_move_comment_and_confluence():
    fake = Fake()
    c = connections(fake)
    asyncio.run(c.connect("jira", site=SITE, email="me@acme.test", token="t"))
    jira = Jira(c)
    [issue] = asyncio.run(jira.mine())
    assert issue == {
        "key": "APP-7",
        "summary": "Fix login",
        "status": "To Do",
        "done": False,
        "priority": "High",
        "type": "Bug",
        "project": "APP",
        "assignee": "",
        "due": "2026-10-05",
        "updated": None,
        "url": f"https://{SITE}/browse/APP-7",
    }
    assert "currentUser()" in fake.seen[-1].url.params["jql"]
    made = asyncio.run(jira.create("app", "Add dark mode", "From the email:\nusers want it"))
    body = json.loads(fake.seen[-1].content)["fields"]
    assert made == {"key": "APP-8", "url": f"https://{SITE}/browse/APP-8"}
    assert body["project"] == {"key": "APP"} and body["assignee"] == {"accountId": "acc-1"}
    assert adf_text(body["description"]) == "From the email:\nusers want it\n"
    assert asyncio.run(jira.move("app-7", "in progress")) == {
        "key": "APP-7",
        "status": "In Progress",
    }
    assert fake.transitions_done == [{"transition": {"id": "21"}}]
    with pytest.raises(ValueError, match="can move to: Done, In Progress"):
        asyncio.run(jira.move("APP-7", "Archived"))
    with pytest.raises(ValueError):
        asyncio.run(jira.comment("../../admin", "hi"))
    asyncio.run(jira.comment("APP-7", "Done on my side"))
    [page] = asyncio.run(jira.pages('onboarding" OR 1=1'))
    assert page["title"] == "Onboarding" and page["excerpt"] == "Steps to start"
    assert fake.seen[-1].url.path == "/wiki/rest/api/search"
    # The user's own quote marks are removed, so the words can't break out of the search.
    assert fake.seen[-1].url.params["cql"] == (
        'type = page AND text ~ "onboarding  OR 1=1" ORDER BY lastmodified DESC'
    )


def test_read_only_connections_cant_act():
    fake = Fake()
    c = connections(fake)
    asyncio.run(c.connect("jira", site=SITE, email="me@acme.test", token="t", actions=False))
    with pytest.raises(IntegrationError, match="read-only"):
        asyncio.run(Jira(c).create("APP", "Nope"))


def test_github_reviews_checks_and_issues_with_the_gh_sign_in(tmp_path, monkeypatch):
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\n[ "$1 $2" = "auth token" ] && echo gho_from_cli\n')
    gh.chmod(0o755)
    monkeypatch.setattr(tokens_module, "GH_CANDIDATES", (str(gh),))
    fake = Fake()
    c = connections(fake)
    assert asyncio.run(c.connect("github", use_cli=True))["identity"] == "mpano"
    assert fake.seen[-1].headers["Authorization"] == "Bearer gho_from_cli"
    github = GitHub(c)
    [review] = asyncio.run(github.reviews())
    assert review["author"] == "ana" and review["repo"] == "acme/web"
    assert "review-requested:@me" in fake.seen[-1].url.params["q"]
    [mine] = asyncio.run(github.my_prs())
    assert mine["checks"] == "failing"
    made = asyncio.run(
        github.create_issue("https://github.com/acme/web", "Broken link", "On /pricing")
    )
    assert made["number"] == 43
    with pytest.raises(ValueError):
        asyncio.run(github.create_issue("acme/web/../../orgs", "x"))
    assert "gho_from_cli" not in json.dumps(asyncio.run(c.catalog()))


def test_adf_round_trip():
    assert adf_text(adf("one\n\ntwo")) == "one\n\ntwo\n"


def make_app(tmp_path, fake, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db",
        api_token="token",
        integrations_enabled=True,
        remote_tool_results="allowlist",
        remote_tool_result_allowlist=["email_search", "slack_search"],
    )
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = agent.proactive = None
    agent.accounts.catalog = AsyncMock(
        return_value={"enabled": True, "providers": [], "accounts": []}
    )
    work = connections(fake)
    agent.tokens, agent.jira, agent.github = work, Jira(work), GitHub(work)
    return TestClient(create_app(settings, agent, enable_ui=True)), agent, settings


def test_connections_screen_and_sharing_follow_your_privacy_choice(tmp_path, monkeypatch):
    client, agent, settings = make_app(tmp_path, Fake(), monkeypatch)
    headers = {"Authorization": "Bearer token"}
    with client:
        listed = client.get("/api/v1/connections", headers=headers).json()
        assert {p["provider"] for p in listed["providers"]} == {"jira", "github"}
        bad = client.post(
            "/api/v1/connections/token",
            json={"provider": "jira", "site": "x.com", "email": "a@b.test", "token": "t"},
            headers=headers,
        )
        assert bad.status_code == 400
        done = client.post(
            "/api/v1/connections/token",
            json={
                "provider": "jira",
                "site": SITE,
                "email": "me@acme.test",
                "token": "secret-token",
            },
            headers=headers,
        ).json()
        assert done == {"connected": True, "identity": "me@acme.test", "shared": True}
        # You share results from your services, so Jira's reads are shared the same way.
        assert "jira_my_issues" in settings.remote_tool_result_allowlist
        assert "jira_my_issues" in agent.planner.privacy.allowed_tools
        assert "jira_create" not in agent.planner.privacy.allowed_tools
        assert "jira_my_issues" in (tmp_path / ".env").read_text()
        listed = client.get("/api/v1/connections", headers=headers).json()
        assert "secret-token" not in json.dumps(listed)
        assert listed["accounts"][0]["scopes"] == ["read", "actions"]
        work = client.get("/api/v1/work", headers=headers).json()
        assert work["jira"]["issues"][0]["key"] == "APP-7" and not work["github"]["connected"]
        gone = client.post(
            "/api/v1/connections/disconnect", json={"account_id": "jira"}, headers=headers
        )
        assert gone.status_code == 200
        assert client.get("/api/v1/work", headers=headers).json()["jira"] == {"connected": False}


def test_writes_ask_first_and_reads_dont(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    registry = agent.executor.registry
    for name in (
        "jira_create",
        "jira_comment",
        "jira_move",
        "github_create_issue",
        "github_comment",
    ):
        assert registry.get(name).risk == RiskLevel.CONFIRM
    for name in ("jira_my_issues", "confluence_search", "github_reviews", "github_read"):
        assert registry.get(name).risk == RiskLevel.SAFE


def test_morning_brief_picks_up_reviews_and_urgent_tickets(tmp_path):
    fake = Fake()
    c = connections(fake)
    asyncio.run(c.connect("jira", site=SITE, email="me@acme.test", token="t"))
    asyncio.run(c.connect("github", token="ghp_x"))
    llm = AsyncMock()
    llm.complete.side_effect = RuntimeError("model down")  # Falls back to its own order.
    briefs = Briefs(
        tmp_path / "db",
        llm=llm,
        proactive_store=ProactiveStore(tmp_path / "db"),
        clock=lambda: datetime(2026, 10, 5, 9, 30).astimezone(),
    )
    briefs.jira, briefs.github = Jira(c), GitHub(c)
    brief = asyncio.run(briefs.morning())
    titles = [item["title"] for item in brief["top"]]
    assert titles == ["Review: Add pricing page", "APP-7 Fix login"]
    assert brief["top"][0]["ref"]["url"] == "https://github.com/acme/web/pull/42"
