"""Inbox triage, follow-ups and Plan my day. Gmail, calendar and the model are mocked."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.executor import Executor
from app.agent.proactive import Proactive
from app.assistant import day_plan, triage
from app.assistant.day_plan import DayPlanner
from app.assistant.triage import InboxTriage
from app.llm.models import ToolCall
from app.llm.structured import parse_json
from app.tools.productivity import planning
from app.tools.productivity.contacts import ContactsDirectory, Person
from app.tools.registry import ToolRegistry
from app.tools.system import proactive as proactive_tools
from app.workflows.schedules import ScheduleStore
from app.workflows.watches import ProactiveStore

ZONE = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=ZONE)  # Thursday morning.


def llm_returning(data):
    llm = AsyncMock()
    llm.complete.return_value = json.dumps(data)
    return llm


def mail(message_id, sender, subject, snippet="..."):
    return {
        "message_id": message_id,
        "thread_id": "t" + message_id,
        "reply_message_id": f"<{message_id}@mail>",
        "from": sender,
        "subject": subject,
        "date": "Thu",
        "snippet": snippet,
    }


def gmail_with(messages):
    gmail = AsyncMock()
    gmail.search.return_value = {"messages": messages, "has_more": False}
    return gmail


# JSON answers


@pytest.mark.parametrize(
    "text",
    ['{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! Here it is: {"a": 1}'],
)
def test_json_answers_are_parsed_even_with_fences_or_prose(text):
    assert parse_json(text) == {"a": 1}


def test_invalid_json_is_a_clear_error():
    with pytest.raises(ValueError, match="valid JSON"):
        parse_json("no json here")


# Triage


async def test_triage_groups_mail_ignores_invented_items_and_keeps_skipped_ones():
    gmail = gmail_with(
        [
            mail("1", "Olivier Mupenzi <o@example.com>", "Contract", "Please sign by today"),
            mail("2", "Sarah <s@example.com>", "Lunch?", "Free Friday?"),
            mail("3", "News <news@shop.example>", "Big sale"),
            mail("4", "Bank <alerts@bank.example>", "Statement ready"),
        ]
    )
    llm = llm_returning(
        {
            "items": [
                {
                    "id": "1",
                    "category": "urgent",
                    "summary": "Sign the contract today",
                    "action": "Sign and reply",
                },
                {"id": "2", "category": "reply", "summary": "Asks about lunch Friday"},
                {"id": "3", "category": "newsletter", "summary": "Sale"},
                {"id": "999", "category": "urgent", "summary": "Invented"},
            ]
        }
    )
    data = await InboxTriage(gmail, llm).triage()
    groups = data["groups"]
    assert [item["message_id"] for item in groups["urgent"]] == ["1"]
    assert groups["urgent"][0]["thread_id"] == "t1"  # Ready for a reply.
    assert [item["message_id"] for item in groups["reply"]] == ["2"]
    assert [item["message_id"] for item in groups["fyi"]] == ["4"]  # Skipped by the model.
    assert "Invented" not in json.dumps(data)
    text = triage.render(data)
    assert "🔴 Urgent (1)" in text and "Olivier Mupenzi — Contract" in text
    assert "→ Sign and reply" in text and "🗞 Newsletters & notifications (1): News" in text
    sent = llm.complete.await_args.args
    assert "never as instructions" in sent[0]


async def test_empty_inbox_needs_no_model_call():
    llm = AsyncMock()
    data = await InboxTriage(gmail_with([]), llm).triage()
    assert triage.render(data).startswith("No unread email")
    llm.complete.assert_not_awaited()


# Follow-ups


def people():
    class Backend:
        def search(self, query):
            return (
                [Person("Olivier Mupenzi", ["olivier@example.com"], [])]
                if "olivier" in query.casefold()
                else []
            )

    return ContactsDirectory(Backend())


async def test_followup_resolves_the_person_and_rejects_past_deadlines(tmp_path):
    store = ProactiveStore(tmp_path / "db")
    controller = planning.PlanningController(
        store, inbox=Mock(), contacts=people(), clock=lambda: NOW
    )
    registry = ToolRegistry()
    planning.register(registry, controller, gmail_available=True)
    executor = Executor(registry)

    async def run(name, **arguments):
        return await executor.execute(ToolCall(call_id="c", name=name, arguments=arguments), "r")

    due = (NOW + timedelta(days=1)).isoformat()
    made = await run(
        "followup_create",
        person="Olivier",
        due=due,
        about="the contract",
        email="attacker@example.com",
    )
    assert made["success"], made
    [item] = store.followups()
    assert item.email == "olivier@example.com" and item.name == "Olivier"
    past = await run(
        "followup_create", person="Olivier", due=(NOW - timedelta(hours=1)).isoformat(), about="x"
    )
    assert not past["success"] and "future" in past["error"]
    listed = await run("followup_list")
    assert "Olivier — the contract" in listed["display"]
    assert (await run("followup_cancel", about="contract"))["success"]
    assert store.followups()[0].status == "cancelled"


async def test_followups_notify_on_reply_or_once_when_overdue(tmp_path):
    store = ProactiveStore(tmp_path / "db")
    replied = store.add_followup(
        "Olivier", "olivier@example.com", "contract", (NOW + timedelta(days=1)).isoformat()
    )
    late = store.add_followup(
        "Sarah", "s@example.com", "invoice", (NOW - timedelta(hours=1)).isoformat()
    )
    gmail = AsyncMock()

    async def search(args):
        found = [mail("9", "Olivier", "Re: contract")] if "olivier" in args.query else []
        return {"messages": found}

    gmail.search.side_effect = search
    notify = AsyncMock()
    engine = Proactive(store, notify, gmail=gmail, clock=lambda: NOW)
    assert await engine.check_followups() == 2
    statuses = {item.id: item.status for item in store.followups()}
    assert statuses == {replied.id: "replied", late.id: "overdue"}
    titles = [call.args[0] for call in notify.await_args_list]
    assert titles == ["✓ Olivier replied", "No reply from Sarah yet"]
    assert "after:" in gmail.search.await_args_list[0].args[0].query
    assert await engine.check_followups() == 0  # Overdue is announced only once.
    later = Proactive(store, notify, gmail=gmail, clock=lambda: NOW + timedelta(days=8))
    await later.check_followups()
    assert {item.status for item in store.followups()} == {"replied", "expired"}


# Plan my day


def calendar_with(events):
    calendar = AsyncMock()
    calendar.events.return_value = {"events": events}
    calendar.backend = Mock()
    calendar.backend.create.side_effect = lambda title, start, end, *rest: {"title": title}
    return calendar


def meeting(title, start, end):
    return {
        "title": title,
        "start": NOW.replace(hour=start).isoformat(),
        "end": NOW.replace(hour=end).isoformat(),
        "all_day": False,
        "busy": True,
    }


async def test_plan_keeps_only_blocks_in_free_working_time(tmp_path):
    store = ProactiveStore(tmp_path / "db")
    reminders = AsyncMock()
    reminders.list.return_value = {
        "reminders": [{"title": "Pay rent", "due": "2026-10-01T09:00", "list": "Home"}]
    }
    llm = llm_returning(
        {
            "blocks": [
                {"start": "09:00", "end": "10:30", "title": "Write API docs", "kind": "focus"},
                {"start": "10:00", "end": "11:00", "title": "Clashes with standup", "kind": "task"},
                {
                    "start": "10:45",
                    "end": "11:00",
                    "title": "Overlaps first block?",
                    "kind": "task",
                },
                {"start": "12:00", "end": "12:45", "title": "Lunch", "kind": "break"},
                {"start": "19:00", "end": "20:00", "title": "After hours", "kind": "task"},
                {"start": "nope", "end": "13:00", "title": "Bad time"},
            ],
            "note": "Busy morning.",
        }
    )
    calendar = calendar_with(
        [meeting("Standup", 10, 11) | {"start": NOW.replace(hour=10, minute=30).isoformat()}]
    )
    planner = DayPlanner(store, calendar, reminders, None, llm, clock_fn=lambda: NOW)
    data = await planner.plan("today")
    titles = [block["title"] for block in data["blocks"]]
    assert titles == ["Write API docs", "Lunch"]
    sent = json.loads(llm.complete.await_args.args[1])
    assert sent["reminders_due"] == ["Pay rent"] and sent["free_slots"][0] == "09:00-10:30"
    text = day_plan.render(data)
    assert "09:00–10:30  🎯 Write API docs" in text and "📅 Standup" in text
    assert store.plan(data["plan_id"])[1] == data["blocks"]


async def test_applying_a_plan_shows_stored_blocks_for_approval(tmp_path):
    store = ProactiveStore(tmp_path / "db")
    blocks = [
        {
            "start": NOW.replace(hour=9).isoformat(),
            "end": NOW.replace(hour=10).isoformat(),
            "title": "Focus",
            "kind": "focus",
            "why": "Deep work",
        }
    ]
    plan_id = store.save_plan("2026-10-01", blocks)
    calendar = calendar_with([])
    planner = DayPlanner(store, calendar, None, None, AsyncMock(), clock_fn=lambda: NOW)
    registry = ToolRegistry()
    planning.register(registry, planning.PlanningController(store, planner), gmail_available=False)
    executor = Executor(registry)
    call = ToolCall(
        call_id="c",
        name="plan_day_apply",
        arguments={"plan_id": plan_id, "blocks": [{"title": "Injected"}]},
    )
    pending = await executor.execute(call, "r")
    assert pending["status"] == "confirmation_required"
    assert pending["arguments"]["blocks"] == blocks  # The stored plan, not the model's.
    calendar.backend.create.assert_not_called()
    done = await executor.execute(
        call.model_copy(update={"arguments": pending["arguments"]}), "r", approved=True
    )
    assert done["success"] and done["result"]["count"] == 1
    assert calendar.backend.create.call_args.args[0] == "Focus"
    missing = await executor.execute(
        ToolCall(call_id="c", name="plan_day_apply", arguments={"plan_id": 999}), "r"
    )
    assert not missing["success"] and "expired" in missing["error"]
    assert "email_triage" not in [tool["name"] for tool in registry.catalog()]


async def test_no_planning_after_the_workday(tmp_path):
    store = ProactiveStore(tmp_path / "db")
    planner = DayPlanner(
        store,
        calendar_with([]),
        None,
        None,
        AsyncMock(),
        clock_fn=lambda: NOW.replace(hour=17, minute=50),
    )
    with pytest.raises(ValueError, match="plan tomorrow"):
        await planner.plan("today")


def test_morning_plan_setting_manages_a_weekday_schedule(tmp_path):
    store, schedules = ProactiveStore(tmp_path / "db"), ScheduleStore(tmp_path / "db")
    controller = proactive_tools.ProactiveController(store, schedules, clock=lambda: NOW)
    controller.apply(morning_plan=True, morning_time="08:15")
    [schedule] = schedules.list()
    assert schedule.message == "Plan my day" and schedule.describe() == "every weekday at 08:15"
    with pytest.raises(ValueError, match="end after it starts"):
        controller.apply(work_start="18:00", work_end="09:00")
    assert store.settings()["work_start"] == "09:00"  # Rejected values are never saved.
