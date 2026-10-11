"""The weekly report: gathering the week, writing, editing, sending, Fridays. All mocked."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.assistant.reports import Reports, week_of
from app.integrations.work import GitHub
from app.workflows.watches import ProactiveStore

ZONE = timezone(timedelta(hours=2))
FRIDAY = datetime(2026, 10, 9, 16, 5, tzinfo=ZONE)  # Week 41: Mon 5 Oct – Fri 9 Oct.


class FakeGitHub:
    _item = staticmethod(GitHub._item)

    def __init__(self):
        self.queries = []
        self.c = SimpleNamespace(request=self.request)

    async def connected(self):
        return True

    async def request(self, provider, method, path, params=None, **kwargs):
        query = params["q"]
        self.queries.append(query)
        repo = "https://api.github.com/repos/acme/app"
        if "is:merged" in query:
            return {
                "items": [
                    {
                        "number": 412,
                        "title": "Faster sync",
                        "repository_url": repo,
                        "html_url": "https://gh/412",
                    }
                ]
            }
        if "reviewed-by" in query:
            return {"items": [{"number": 7, "title": "Docs", "repository_url": repo}] * 2}
        return {"items": []}

    async def my_prs(self):
        return [
            {
                "repo": "acme/app",
                "number": 420,
                "title": "Dark mode",
                "url": "u",
                "checks": "failing",
            }
        ]


class FakeJira:
    async def connected(self):
        return True

    async def search(self, jql, limit=15):
        if "Done" in jql:
            assert 'resolved >= "2026-10-05"' in jql
            return [{"key": "ABC-9", "summary": "Login bug", "status": "Done", "url": "j/9"}]
        return [{"key": "ABC-12", "summary": "Billing page", "status": "In Progress", "url": ""}]


class FakePromises:
    def items(self, status, since=0.0):
        if status == "done":
            return [
                {"direction": "mine", "person": "Olivier", "what": "send the budget"},
                {"direction": "theirs", "person": "Sam", "what": "share the logo"},
            ]
        return [
            {"direction": "mine", "person": "Ana", "what": "review the plan", "due": "2026-10-12"},
            {"direction": "mine", "person": "Ana", "what": "someday", "due": "2026-12-01"},
        ]


def calendar():
    found = AsyncMock()
    found.events.return_value = {
        "events": [
            {"start": "2026-10-05T10:00:00+02:00", "end": "2026-10-05T11:30:00+02:00"},
            {"start": "2026-10-06T09:00:00+02:00", "end": "2026-10-06T10:00:00+02:00"},
            {
                "start": "2026-10-07T00:00:00+02:00",
                "end": "2026-10-08T00:00:00+02:00",
                "all_day": True,
            },
        ]
    }
    return found


def reports(tmp_path, llm=None, now=FRIDAY, **kwargs):
    store = ProactiveStore(tmp_path / "db")
    made = Reports(
        tmp_path / "db",
        llm=llm,
        proactive_store=store,
        commitments=SimpleNamespace(store=FakePromises()),
        calendar=calendar(),
        github=FakeGitHub(),
        jira=FakeJira(),
        clock=lambda: now,
        **kwargs,
    )
    made.notify = AsyncMock()
    return made, store


async def test_the_week_is_gathered_from_everything_bridge_sees(tmp_path):
    made, _ = reports(tmp_path)
    facts = await made.gather(week_of(FRIDAY.date())[1])
    assert facts["shipped"] == [
        {"ref": "app#412", "repo": "acme/app", "title": "Faster sync", "url": "https://gh/412"}
    ]
    assert len(facts["reviewed"]) == 2 and facts["open_prs"][0]["checks"] == "failing"
    assert facts["tickets_done"][0]["ref"] == "ABC-9"
    assert facts["tickets_in_progress"][0]["status"] == "In Progress"
    assert facts["promises_kept"] == [{"person": "Olivier", "what": "send the budget"}]
    assert facts["promises_received"] == [{"person": "Sam", "what": "share the logo"}]
    assert [item["what"] for item in facts["coming_up"]] == ["review the plan"]
    assert facts["meetings"] == {"count": 2, "hours": 2.5}  # All-day events don't count.
    assert all(
        "merged:>=2026-10-05" in q or "updated:>=2026-10-05" in q or "closed:>=" in q
        for q in made.github.queries
    )
    labels = [f"{s['count']:g} {s['label']}" for s in made.stats(facts)]
    assert labels == [
        "1 PRs merged",
        "2 PRs reviewed",
        "1 tickets done",
        "1 promises kept",
        "2.5 hours in meetings",
    ]


async def test_the_model_writes_it_without_links_and_falls_back_to_a_list(tmp_path):
    llm = AsyncMock()
    llm.complete.return_value = "Done this week\n• Shipped app#412"
    made, _ = reports(tmp_path, llm)
    report = await made.write()
    assert report["text"] == "Done this week\n• Shipped app#412" and report["week"] == "2026-W41"
    instructions, data = llm.complete.await_args.args
    assert "Never invent" in instructions and "https://gh/412" not in data
    assert json.loads(data)["shipped"][0]["ref"] == "app#412"
    assert report["links"][0] == {"ref": "app#412", "url": "https://gh/412"}
    assert (await made.write())["text"] == report["text"] and llm.complete.await_count == 1

    llm.complete.side_effect = RuntimeError("down")
    plain = (await made.write(fresh=True))["text"]
    assert plain.startswith("Done this week\n• Merged app#412: Faster sync")
    assert "In progress\n• ABC-12: Billing page" in plain and "Next week" in plain


async def test_a_quiet_week_needs_no_model(tmp_path):
    llm = AsyncMock()
    made, _ = reports(tmp_path, llm)
    made.github = made.jira = made.calendar = None
    made.commitments = None
    report = await made.write()
    assert report["text"].startswith("A quiet week") and report["stats"] == []
    llm.complete.assert_not_awaited()


async def test_edit_and_send_exactly_whats_shown(tmp_path):
    slack = AsyncMock()
    slack.send.return_value = {"destination": "#team"}
    gmail = AsyncMock()
    made, store = reports(tmp_path, slack=slack, gmail=gmail)
    with pytest.raises(ValueError, match="Write the report first"):
        await made.send("2026-W41", "hi", slack="#team")
    await made.write()
    assert made.edit("2026-W41", "  My week  ")["edited"] is True
    with pytest.raises(ValueError, match="one place"):
        await made.send("2026-W41", "x", slack="#team", email=["a@b.co"])
    sent = await made.send("2026-W41", "Final text", slack="#team")
    args = slack.send.await_args.args[0]
    assert args.channel == "#team" and args.text == "Final text" and args.thread_ts is None
    assert sent["text"] == "Final text" and sent["sent"][0]["to"] == "#team"
    assert store.settings()["weekly_report_to"] == "#team" and sent["destination"] == "#team"
    await made.send("2026-W41", "Final text", email=["lead@acme.co"])
    mail = gmail.send.await_args.args[0]
    assert mail.to == ["lead@acme.co"] and mail.subject == "Weekly update — week of 5 Oct"
    assert len(made.view()["sent"]) == 2


async def test_friday_afternoon_draft_and_notification_once(tmp_path):
    made, _ = reports(tmp_path, AsyncMock(complete=AsyncMock(return_value="Report")))
    await made.check()
    title, body, view = made.notify.await_args.args
    assert title == "Your weekly report is ready" and view == "today"
    assert body.startswith("1 PRs merged, 2 PRs reviewed, 1 tickets done.")
    await made.check()
    assert made.notify.await_count == 1

    for name, now, on in (
        ("thursday", FRIDAY - timedelta(days=1), True),
        ("too-early", FRIDAY.replace(hour=15), True),
        ("too-late", FRIDAY.replace(hour=21), True),
        ("turned-off", FRIDAY, False),
    ):
        folder = tmp_path / name
        folder.mkdir()
        other, other_store = reports(folder, now=now)
        other_store.update_settings(weekly_report=on)
        await other.check()
        other.notify.assert_not_awaited()
        assert other.saved("2026-W41") is None, name


def test_which_week_and_when_it_shows(tmp_path):
    made, _ = reports(tmp_path)
    assert made.current_week()[0] == "2026-W41" and made.due() is True
    monday = FRIDAY + timedelta(days=3)
    assert reports(tmp_path, now=monday.replace(hour=9))[0].current_week()[0] == "2026-W41"
    assert reports(tmp_path, now=monday.replace(hour=13))[0].current_week()[0] == "2026-W42"
    assert reports(tmp_path, now=monday.replace(hour=13))[0].due() is False
    assert reports(tmp_path, now=FRIDAY.replace(hour=12))[0].due() is False
    assert made.view()["range"] == "5 Oct – 9 Oct"


def test_report_routes(tmp_path, monkeypatch):
    from app.api.server import create_app
    from app.bootstrap import build_agent
    from app.config.settings import Settings

    monkeypatch.chdir(tmp_path)
    config = Settings(
        _env_file=None, database_path=tmp_path / "db", api_token="token", openai_api_key="sk-x"
    )
    agent = build_agent(config, llm=AsyncMock())
    agent.reports.llm = SimpleNamespace(complete=AsyncMock(return_value="My week"))
    agent.reports.github = agent.reports.jira = agent.reports.calendar = None
    agent.reports.commitments = SimpleNamespace(store=FakePromises())
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(config, agent, enable_ui=True)) as client:
        assert client.get("/api/v1/report", headers=headers).json()["text"] is None
        week = client.post("/api/v1/report/write", json={}, headers=headers).json()
        assert week["text"] == "My week"
        edited = client.post(
            "/api/v1/report/edit", json={"week": week["week"], "text": "Edited"}, headers=headers
        )
        assert edited.json()["text"] == "Edited"
        nowhere = client.post(
            "/api/v1/report/send", json={"week": week["week"], "text": "x"}, headers=headers
        )
        assert nowhere.status_code == 422
        assert client.get("/api/v1/report").status_code == 401
        assert "BridgeReport" in client.get("/ui/report.js").text
        settings = client.get("/api/v1/automations", headers=headers).json()["settings"]
        assert settings["weekly_report"] is True and settings["weekly_report_time"] == "16:00"
