"""Focus mode: calendar, Slack and Spotify are faked; nothing is sent anywhere."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.executor import Executor
from app.assistant.focus import SLACK_RECONNECT, FocusMode, FocusStore
from app.integrations.models import IntegrationError
from app.llm.models import ToolCall
from app.security.risk import RiskLevel
from app.tools.productivity import focus as focus_tools
from app.tools.registry import ToolRegistry

START = datetime(2026, 10, 1, 14, 0).astimezone().timestamp()


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


class Slack:
    """Records Slack Web API calls made through AccountManager.request."""

    def __init__(self, missing_scope=False):
        self.calls, self.missing_scope = [], missing_scope

    async def request(self, account_id, provider, verb, method, scopes=(), **kwargs):
        if self.missing_scope and set(scopes) & {"users.profile:read", "users.profile:write"}:
            raise IntegrationError("This connection lacks the required permissions.")
        self.calls.append((method, kwargs))
        if method == "users.profile.get":
            return {"profile": {"status_text": "In Kigali", "status_emoji": ":sunny:"}}
        return {"ok": True}

    async def list_accounts(self):
        return {"accounts": [{"provider": "slack", "identity": "T1/U42"}]}


def make(tmp_path, clock, slack=None):
    calendar = SimpleNamespace(backend=Mock())
    calendar.backend.create.side_effect = lambda title, *rest: {"title": title}
    spotify_web = AsyncMock()
    spotify_web.search.return_value = {
        "items": [{"name": "Deep Focus", "uri": "spotify:playlist:abc", "artists": []}]
    }
    desktop = AsyncMock()
    desktop.now_playing.return_value = {"state": "playing"}
    slack_search = AsyncMock()
    slack_search.search.return_value = {
        "messages": [
            {"user": "olivier", "ts": str(START + 600)},  # During focus.
            {"user": "sarah", "ts": str(START - 3600)},  # Before it started.
        ]
    }
    gmail = AsyncMock()
    gmail.search.return_value = {"messages": [{"from": "Sarah Uwase <s@example.com>"}]}
    notify = AsyncMock()
    accounts = slack or Slack()
    mode = FocusMode(
        FocusStore(tmp_path / "db"),
        notify,
        calendar=calendar,
        accounts=accounts,
        spotify_web=spotify_web,
        spotify_desktop=desktop,
        gmail=gmail,
        slack=slack_search,
        clock=clock,
    )
    return mode, SimpleNamespace(
        calendar=calendar, slack=accounts, desktop=desktop, notify=notify, gmail=gmail
    )


async def test_start_blocks_calendar_quiets_slack_and_plays_music(tmp_path):
    clock = Clock()
    mode, fakes = make(tmp_path, clock)
    data = await mode.start(90, "API docs")
    assert data["until"] == "15:30" and data["skipped"] == []
    assert len(data["done"]) == 3
    title = fakes.calendar.backend.create.call_args.args[0]
    assert title == "🎯 Focus: API docs"
    methods = [method for method, _ in fakes.slack.calls]
    assert methods == ["users.profile.get", "users.profile.set", "dnd.setSnooze"]
    profile = fakes.slack.calls[1][1]["json"]["profile"]
    assert profile["status_emoji"] == ":dart:" and profile["status_expiration"] == int(START + 5400)
    assert fakes.slack.calls[2][1]["params"] == {"num_minutes": 90}
    fakes.desktop.play_uri.assert_awaited_once_with("spotify:playlist:abc")
    assert mode.status() == {
        "active": True,
        "task": "API docs",
        "until": "15:30",
        "minutes_left": 90,
    }
    with pytest.raises(ValueError, match="already focusing until 15:30"):
        await mode.start(30)
    await mode.stop()


async def test_missing_slack_permission_skips_only_slack(tmp_path):
    mode, fakes = make(tmp_path, Clock(), slack=Slack(missing_scope=True))
    data = await mode.start(60)
    assert data["skipped"] == [f"Slack: {SLACK_RECONNECT}"]
    assert len(data["done"]) == 2  # Calendar and music still happened.
    await mode.stop()


async def test_stopping_early_restores_everything_and_reports_what_was_missed(tmp_path):
    clock = Clock()
    mode, fakes = make(tmp_path, clock)
    await mode.start(90, "API docs")
    fakes.slack.calls.clear()
    clock.now = START + 30 * 60
    data = await mode.stop()
    assert data["status"] == "stopped" and data["minutes"] == 30
    assert data["slack_mentions"] == 1 and data["emails"] == 1
    assert data["summary"] == (
        "While you focused: 1 Slack mention (olivier) and 1 new email (Sarah Uwase)."
    )
    restored = fakes.slack.calls[0][1]["json"]["profile"]
    assert restored["status_text"] == "In Kigali"  # The status from before focus.
    assert [method for method, _ in fakes.slack.calls] == ["users.profile.set", "dnd.endSnooze"]
    fakes.desktop.control.assert_awaited_once()
    fakes.calendar.backend.delete.assert_called_once()
    shortened = fakes.calendar.backend.create.call_args.args
    assert shortened[2].timestamp() == START + 30 * 60  # Kept the 30 minutes spent.
    fakes.notify.assert_awaited_once()
    assert fakes.notify.await_args.args[0] == "Focus stopped"
    assert "after:" in fakes.gmail.search.await_args.args[0].query
    assert mode.status() == {"active": False}
    with pytest.raises(ValueError, match="not in focus"):
        await mode.stop()


async def test_session_ends_on_time_even_after_a_restart(tmp_path):
    clock = Clock()
    mode, fakes = make(tmp_path, clock)
    await mode.start(60)
    mode._timer.cancel()
    restarted, fakes = make(tmp_path, clock)  # A new Bridge process, same database.
    clock.now = START + 61 * 60
    await restarted.check()
    assert restarted.status() == {"active": False}
    assert fakes.notify.await_args.args[0] == "Focus done ✓"
    assert "dnd.endSnooze" not in [method for method, _ in fakes.slack.calls]  # Expired anyway.
    fakes.calendar.backend.delete.assert_not_called()  # The block already ended on time.
    assert (await restarted.finish(1, "done")) == {"status": "already_ended"}


async def test_starting_focus_needs_approval(tmp_path):
    mode, fakes = make(tmp_path, Clock())
    registry = ToolRegistry()
    focus_tools.register(registry, focus_tools.FocusController(mode))
    assert registry.get("focus_start").risk == RiskLevel.CONFIRM
    call = ToolCall(call_id="c", name="focus_start", arguments={"minutes": 45, "task": "docs"})
    pending = await Executor(registry).execute(call, "r")
    assert pending["status"] == "confirmation_required"
    fakes.calendar.backend.create.assert_not_called()
    status = await Executor(registry).execute(
        ToolCall(call_id="s", name="focus_status", arguments={}), "r"
    )
    assert status["display"] == "You're not in focus mode."


def test_menu_bar_shows_the_countdown(tmp_path):
    from app.desktop.menubar import MenuBarController
    from app.desktop.service import ServiceState, ServiceStatus
    from tests.test_desktop import FakeApp, FakeItem, FakeTimer

    native = SimpleNamespace(
        App=FakeApp, MenuItem=FakeItem, Timer=FakeTimer, alert=Mock(), quit_application=Mock()
    )
    service = Mock()
    service.status = ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")
    session = {"end": __import__("time").time() + 95 * 60 - 5}
    menu = MenuBarController(service, native, Mock(), focus_reader=lambda: session)
    menu.refresh()
    assert menu.app.title == "🎯 1h 35m"
    assert menu.focus_item.title == "Stop Focus (1h 35m left)"
    assert menu.focus_item.callback == menu.stop_focus
    session["end"] = 0
    menu._focus_checked = 0
    menu.refresh()
    assert menu.app.title == "Bridge" and menu.focus_item.callback is None
