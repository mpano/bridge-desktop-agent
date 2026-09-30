"""Schedules, the scheduler, briefing, Chrome tabs, Mac controls and notifications (all mocked)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.agent.executor import Executor
from app.agent.scheduler import Scheduler
from app.desktop.service import ServiceState, ServiceStatus
from app.desktop.voice import MenuVoiceService
from app.integrations.models import IntegrationError
from app.llm.models import ToolCall
from app.tools.browser import chrome
from app.tools.productivity import briefing
from app.tools.registry import ToolRegistry
from app.tools.system import mac, schedules
from app.tools.system.notifications import post_notification
from app.workflows.schedules import ScheduleStore, next_occurrence

ZONE = datetime.now().astimezone().tzinfo


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute).astimezone()


def executor_for(module, controller):
    registry = ToolRegistry()
    module.register(registry, controller)
    return Executor(registry)


async def call(executor, tool, approved=False, **arguments):
    return await executor.execute(
        ToolCall(call_id="c", name=tool, arguments=arguments), "r", approved=approved
    )


# Schedule timing


def test_weekday_schedule_skips_weekend_and_today_after_time():
    friday_evening = at(2, 18)  # 2 Oct 2026 is a Friday.
    assert next_occurrence("weekdays", "08:30", None, friday_evening) == at(5, 8, 30)
    assert next_occurrence("daily", "08:30", None, at(2, 7)) == at(2, 8, 30)
    assert next_occurrence("weekly", "10:00", 6, friday_evening) == at(4, 10)
    past = at(1, 9).isoformat()
    assert next_occurrence("once", past, None, at(2, 9)) is None


async def test_schedule_tool_needs_approval_and_validates(tmp_path):
    store = ScheduleStore(tmp_path / "db")
    controller = schedules.ScheduleController(store, clock=lambda: at(1, 7))
    executor = executor_for(schedules, controller)
    args = {"message": "brief me", "repeat": "weekdays", "time": "8:30"}
    pending = await call(executor, "schedule_create", **args)
    assert pending["status"] == "confirmation_required" and store.list() == []
    created = await call(executor, "schedule_create", True, **args)
    assert created["success"] and "every weekday at 08:30" in created["display"]
    listed = await call(executor, "schedule_list")
    assert "#1 “brief me”" in listed["display"]
    deleted = await call(executor, "schedule_delete", matching="BRIEF")
    assert deleted["success"] and store.list() == []
    with pytest.raises(ValidationError):
        schedules.ScheduleCreateInput(message="brief me", repeat="weekly", time="09:00")
    with pytest.raises(ValidationError):
        schedules.ScheduleCreateInput(message="brief me", repeat="daily", time="25:00")


# Scheduler


def fake_agent(result=None, busy=False):
    agent = Mock()
    if busy:
        agent.submit.side_effect = ValueError("busy")
    agent.submit.return_value = {"request_id": "r1"}
    agent.task_progress.return_value = {
        "result": result or {"status": "completed", "message": "ok"}
    }
    return agent


async def test_scheduler_runs_due_request_advances_and_notifies(tmp_path):
    store = ScheduleStore(tmp_path / "db")
    item = store.add("brief me", "daily", "08:30", None, at(1, 7))
    notify, agent = AsyncMock(), fake_agent({"status": "completed", "message": "Your day"})
    scheduler = Scheduler(agent, store, notify, clock=lambda: at(1, 8, 31))
    await scheduler.tick()
    agent.submit.assert_called_once_with("brief me")
    notify.assert_awaited_once()
    assert notify.await_args.args[1] == "Your day"
    saved = store.list()[0]
    assert saved.next_run == at(2, 8, 30).isoformat() and saved.last_status == "completed"
    await scheduler.tick()
    agent.submit.assert_called_once()  # Not due again until tomorrow.
    assert item.id == saved.id


async def test_scheduler_waits_when_busy_and_skips_stale_runs(tmp_path):
    store = ScheduleStore(tmp_path / "db")
    store.add("brief me", "daily", "08:30", None, at(1, 7))
    busy = fake_agent(busy=True)
    await Scheduler(busy, store, AsyncMock(), clock=lambda: at(1, 8, 31)).tick()
    assert store.list()[0].next_run == at(1, 8, 30).isoformat()  # Retried next tick.
    agent, notify = fake_agent(), AsyncMock()
    await Scheduler(agent, store, notify, clock=lambda: at(1, 23)).tick()
    agent.submit.assert_not_called()
    assert store.list()[0].last_status == "missed"


async def test_scheduler_reports_pending_approval(tmp_path):
    store = ScheduleStore(tmp_path / "db")
    store.add("email the team", "daily", "08:30", None, at(1, 7))
    notify = AsyncMock()
    agent = fake_agent({"status": "confirmation_required", "message": "Review"})
    await Scheduler(agent, store, notify, clock=lambda: at(1, 8, 31)).tick()
    assert "approval" in notify.await_args.args[1]


# Briefing


async def test_briefing_combines_sources_and_skips_unavailable_ones():
    now = datetime(2026, 10, 1, 8, 0, tzinfo=timezone(timedelta(hours=2)))
    calendar = AsyncMock()
    calendar.events.return_value = {
        "events": [
            {
                "title": "Standup",
                "start": now.replace(hour=9).isoformat(),
                "end": now.replace(hour=10).isoformat(),
                "all_day": False,
                "location": "",
            }
        ]
    }
    reminders = AsyncMock()
    reminders.list.return_value = {
        "reminders": [
            {"title": "Pay rent", "due": "2026-10-01T09:00", "list": "Home"},
            {"title": "Later", "due": "2026-10-05T09:00", "list": "Home"},
            {"title": "Someday", "due": None, "list": "Home"},
        ]
    }
    gmail = AsyncMock()
    gmail.search.side_effect = IntegrationError("No Gmail account is connected.")
    status = AsyncMock()
    status.status.side_effect = ValueError("pmset unavailable")
    controller = briefing.BriefingController(calendar, reminders, status, gmail, clock=lambda: now)
    result = await call(executor_for(briefing, controller), "daily_briefing")
    text = result["display"]
    assert text.startswith("☀️ Good morning — Thursday 01 October")
    assert "Standup" in text and "Pay rent" in text and "Later" not in text
    assert "Mail" not in text  # Gmail not set up is not worth mentioning.
    assert "Mac skipped: pmset unavailable" in text


# Chrome


LISTING = (
    "1\x1f1\x1fInbox (3) - Gmail\x1fhttps://mail.google.com/\x1ffalse\x1e"
    "1\x1f2\x1fBridge docs\x1fhttps://docs.example/\x1ftrue\x1e"
    "2\x1f1\x1fLofi - YouTube\x1fhttps://youtube.com/watch?v=1\x1ftrue\x1e"
    "2\x1f2\x1fTalk - YouTube\x1fhttps://youtube.com/watch?v=2\x1ffalse\x1e"
)


async def test_chrome_switch_close_and_policies():
    runner = AsyncMock()
    runner.run.return_value = LISTING
    controller = chrome.ChromeController(runner)
    executor = executor_for(chrome, controller)
    switched = await call(executor, "chrome_switch_tab", query="gmail")
    assert switched["success"] and runner.run.await_args.args[3:] == ("1", "1")
    ambiguous = await call(executor, "chrome_switch_tab", query="youtube")
    assert not ambiguous["success"] and "2 tabs match" in ambiguous["error"]
    pending = await call(executor, "chrome_close_tabs", query="youtube")
    assert pending["status"] == "confirmation_required"
    closed = await call(executor, "chrome_close_tabs", True, query="youtube")
    assert closed["success"] and runner.run.await_args.args[3:] == ("2,2", "2,1")
    current = await call(executor, "chrome_close_tabs")
    assert current["success"] and runner.run.await_args.args[3:] == ("1,2",)
    runner.run.return_value = "not_running"
    assert "isn't open" in (await call(executor, "chrome_list_tabs"))["error"]


async def test_chrome_read_page_explains_disabled_javascript():
    runner = AsyncMock()
    runner.run.side_effect = RuntimeError("blocked")
    result = await call(executor_for(chrome, chrome.ChromeController(runner)), "chrome_read_page")
    assert "Allow JavaScript from Apple Events" in result["error"]


# Mac


async def test_mac_status_parses_battery_and_sleep_needs_approval(tmp_path):
    runner = AsyncMock()
    runner.run.side_effect = [
        "Now drawing from 'Battery Power'\n -InternalBattery-0\t18%; discharging; 1:02 remaining",
        "192.168.1.4",
    ]
    executor = executor_for(mac, mac.MacController(runner, home=tmp_path))
    status = await call(executor, "mac_status")
    assert status["result"]["battery_percent"] == 18
    assert "1:02 left" in status["display"] and "Wi-Fi connected" in status["display"]
    pending = await call(executor, "mac_control", action="sleep")
    assert pending["status"] == "confirmation_required"
    runner.run.side_effect = None
    runner.run.return_value = "true"
    dark = await call(executor, "mac_control", action="dark_mode")
    assert dark["success"] and dark["display"] == "✓ Dark mode is on."
    assert runner.run.await_args.args[-1] == "dark"


# Notifications


async def test_notification_text_is_passed_as_arguments():
    runner = AsyncMock()
    await post_notification(runner, 'Title" & do shell script "x', "line one\nline two")
    argv = runner.run.await_args.args
    assert "do shell script" not in argv[2]
    assert argv[3:] == ('Title" & do shell script "x', "line one line two")
    runner.run.side_effect = RuntimeError("no notifications")
    await post_notification(runner, "t", "m")  # Never raises.


async def test_typed_request_notifies_only_when_panel_hidden(monkeypatch):
    local = Mock()
    local.status = ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")

    class FakeAgent:
        def __init__(self, *args):
            pass

        async def message(self, text):
            return {"status": "completed", "message": "Playing now"}

    monkeypatch.setattr("app.desktop.voice.LocalAPIAgent", FakeAgent)
    settings = SimpleNamespace(
        api_token=SimpleNamespace(get_secret_value=lambda: "secret"),
        voice_wake_word_model_path=None,
    )
    voice = MenuVoiceService(settings, local)
    voice.notifier = AsyncMock()
    assert voice.submit_text("play music") and voice.wait(2)
    voice.notifier.assert_not_awaited()
    voice.panel_visible = False
    assert voice.submit_text("play music") and voice.wait(2)
    voice.notifier.assert_awaited_once_with("Bridge · play music", "Playing now")
