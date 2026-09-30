"""Proactive assistant: meeting heads-ups, watches, evening summary. All services mocked."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.executor import Executor
from app.agent.proactive import Proactive, slack_query
from app.llm.models import ToolCall
from app.tools.productivity import briefing
from app.tools.registry import ToolRegistry
from app.tools.system import proactive as proactive_tools
from app.workflows.schedules import ScheduleStore
from app.workflows.watches import ProactiveStore

ZONE = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=ZONE)


def event(title, minutes_from_now, all_day=False, event_id=None):
    start = NOW + timedelta(minutes=minutes_from_now)
    return {
        "id": event_id or title,
        "title": title,
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=30)).isoformat(),
        "all_day": all_day,
        "calendar": "Work",
        "location": "Room 4",
    }


def calendar_with(*events):
    calendar = AsyncMock()
    calendar.events.return_value = {"events": list(events)}
    return calendar


async def test_meeting_heads_up_is_sent_once_and_respects_settings(tmp_path):
    store = ProactiveStore(tmp_path / "db")
    notify = AsyncMock()
    calendar = calendar_with(event("Standup", 8), event("Holiday", 5, all_day=True))
    engine = Proactive(store, notify, calendar=calendar, clock=lambda: NOW)
    assert await engine.check_meetings() == 1
    title, body = notify.await_args.args
    assert title == "In 8 min: Standup" and "09:08–09:38 · Room 4" in body
    assert await engine.check_meetings() == 0  # Never twice for the same meeting.
    restarted = Proactive(store, notify, calendar=calendar, clock=lambda: NOW)
    assert await restarted.check_meetings() == 0  # Not even after a restart.
    store.update_settings(meeting_prep=False)
    engine2 = Proactive(
        store,
        AsyncMock(),
        calendar=calendar_with(event("Later", 3, event_id="x")),
        clock=lambda: NOW,
    )
    assert await engine2.check_meetings() == 0


async def test_meetings_outside_the_lead_time_wait(tmp_path):
    store = ProactiveStore(tmp_path / "db")
    notify = AsyncMock()
    engine = Proactive(
        store, notify, calendar=calendar_with(event("Review", 25)), clock=lambda: NOW
    )
    assert await engine.check_meetings() == 0
    store.update_settings(lead_minutes=30)
    assert await engine.check_meetings() == 1


def gmail_with(messages):
    gmail = AsyncMock()
    gmail.search.return_value = {"messages": messages}
    return gmail


def mail(message_id, subject):
    return {"message_id": message_id, "from": "Olivier Mupenzi <o@example.com>", "subject": subject}


async def test_email_watch_ignores_existing_mail_then_notifies_new_mail(tmp_path):
    store = ProactiveStore(tmp_path / "db")
    store.add_watch("email", "from:olivier", "Emails from Olivier")
    notify = AsyncMock()
    gmail = gmail_with([mail("m1", "Old news")])
    engine = Proactive(store, notify, gmail=gmail, clock=lambda: NOW)
    assert await engine.check_watches() == 0  # First look only records what's there.
    gmail.search.return_value = {"messages": [mail("m2", "Dinner Friday?"), mail("m1", "Old news")]}
    assert await engine.check_watches() == 1
    assert notify.await_args.args == (
        "Email from Olivier Mupenzi · Emails from Olivier",
        "Dinner Friday?",
    )
    assert await engine.check_watches() == 0
    query = gmail.search.await_args.args[0].query
    assert query == "from:olivier newer_than:2d"


async def test_watch_problems_are_recorded_not_raised(tmp_path):
    store = ProactiveStore(tmp_path / "db")
    store.add_watch("slack", "mentions", "Slack mentions of me")
    engine = Proactive(store, AsyncMock(), clock=lambda: NOW)
    assert await engine.check_watches() == 0
    assert store.watches()[0].last_error == "Slack isn't connected."


def test_slack_mentions_use_the_connected_user_id():
    assert slack_query("mentions", "T0123/U0456") == "<@U0456>"
    assert slack_query("release notes", "T0123/U0456") == "release notes"
    assert slack_query("mentions", None) == "mentions"


async def test_evening_summary_setting_manages_one_weekday_schedule(tmp_path):
    store, schedules = ProactiveStore(tmp_path / "db"), ScheduleStore(tmp_path / "db")
    controller = proactive_tools.ProactiveController(store, schedules, clock=lambda: NOW)
    controller.apply(evening_summary=True, evening_time="19:30")
    [schedule] = schedules.list()
    assert (
        schedule.message == "Brief me for tomorrow"
        and schedule.describe() == "every weekday at 19:30"
    )
    controller.apply(evening_time="18:00")
    [schedule] = schedules.list()
    assert schedule.at == "18:00"
    controller.apply(evening_summary=False)
    assert schedules.list() == []


async def test_watch_tool_requires_a_connected_account_and_can_be_stopped(tmp_path):
    store, schedules = ProactiveStore(tmp_path / "db"), ScheduleStore(tmp_path / "db")
    accounts = AsyncMock()
    accounts.list_accounts.return_value = {"accounts": [{"provider": "gmail", "identity": "me"}]}
    registry = ToolRegistry()
    proactive_tools.register(
        registry, proactive_tools.ProactiveController(store, schedules, accounts)
    )
    executor = Executor(registry)

    async def run(name, **arguments):
        return await executor.execute(ToolCall(call_id="c", name=name, arguments=arguments), "r")

    no_slack = await run("watch_create", kind="slack", query="mentions", label="Mentions")
    assert not no_slack["success"] and "Connect Slack" in no_slack["error"]
    created = await run(
        "watch_create", kind="email", query="from:olivier", label="Emails from Olivier"
    )
    assert created["success"] and "I'll let you know" in created["display"]
    listed = await run("watch_list")
    assert "Emails from Olivier (email: from:olivier)" in listed["display"]
    assert (await run("watch_delete", about="olivier"))["success"]
    assert store.watches() == []
    settings = await run("proactive_settings", meeting_prep=False)
    assert "Meeting heads-ups off" in settings["display"]


async def test_briefing_for_tomorrow_covers_the_whole_next_day():
    calendar = calendar_with()
    reminders = AsyncMock()
    reminders.list.return_value = {
        "reminders": [
            {"title": "Due tomorrow", "due": "2026-10-02T09:00", "list": "Home"},
            {"title": "Next week", "due": "2026-10-08T09:00", "list": "Home"},
        ]
    }
    controller = briefing.BriefingController(calendar, reminders, clock=lambda: NOW)
    data = await controller.brief(SimpleNamespace(day="tomorrow"))
    window = calendar.events.await_args.args[0]
    assert window.start.startswith("2026-10-02T00:00") and window.end.startswith("2026-10-03T00:00")
    text = briefing.render(data)
    assert text.startswith("🌙 Tomorrow — Friday 02 October")
    assert "Due tomorrow" in text and "Next week" not in text


@pytest.mark.parametrize("bad", ["25:00", "7pm", "12:60"])
def test_evening_time_must_be_24_hour(bad):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        proactive_tools.SettingsInput(evening_time=bad)
