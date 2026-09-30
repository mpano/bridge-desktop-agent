"""Shortcuts, Reminders and Notes. osascript and shortcuts are mocked; nothing runs on the Mac."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.agent.executor import Executor
from app.llm.models import ToolCall
from app.tools.productivity import calendar_mac, notes, reminders
from app.tools.registry import ToolRegistry
from app.tools.system import shortcuts

ZONE = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=ZONE)


def executor_for(module, controller):
    registry = ToolRegistry()
    module.register(registry, controller)
    return Executor(registry)


async def call(executor, tool, approved=False, **arguments):
    return await executor.execute(
        ToolCall(call_id="c", name=tool, arguments=arguments), "request", approved=approved
    )


# Shortcuts


class ShortcutsRunner:
    def __init__(self, names=("Focus Mode", "Log Water")):
        self.names = names
        self.calls = []

    async def run(self, *argv, **_):
        self.calls.append(argv)
        if argv[1] == "list":
            return "\n".join(self.names)
        output = Path(argv[argv.index("--output-path") + 1])
        received = ""
        if "--input-path" in argv:
            received = Path(argv[argv.index("--input-path") + 1]).read_text()
        output.write_text(f"ran with {received or 'nothing'}")
        return ""


async def test_untrusted_shortcut_needs_approval_and_trusted_runs_directly():
    runner = ShortcutsRunner()
    executor = executor_for(shortcuts, shortcuts.ShortcutsController(runner, ["log water"]))
    pending = await call(executor, "shortcuts_run", name="Focus Mode")
    assert pending["status"] == "confirmation_required" and not runner.calls
    result = await call(executor, "shortcuts_run", name="log water", input="500 ml")
    assert result["success"]
    assert result["result"]["shortcut"] == "Log Water"
    assert "ran with 500 ml" in result["display"]


async def test_unknown_shortcut_is_refused_before_running():
    runner = ShortcutsRunner()
    executor = executor_for(shortcuts, shortcuts.ShortcutsController(runner))
    result = await call(executor, "shortcuts_run", True, name="Delete Everything")
    assert not result["success"] and "No shortcut named" in result["error"]
    assert all(argv[1] == "list" for argv in runner.calls)


async def test_shortcut_list_filters_and_explains_empty():
    executor = executor_for(shortcuts, shortcuts.ShortcutsController(ShortcutsRunner()))
    result = await call(executor, "shortcuts_list", query="water")
    assert result["result"]["shortcuts"] == ["Log Water"]
    empty = executor_for(shortcuts, shortcuts.ShortcutsController(ShortcutsRunner(names=())))
    assert "Shortcuts app" in (await call(empty, "shortcuts_list"))["display"]


# Reminders


async def test_reminder_due_time_is_passed_as_seconds_from_now():
    runner = AsyncMock()
    runner.run.return_value = "Reminders"
    controller = reminders.RemindersController(runner, clock=lambda: NOW)
    executor = executor_for(reminders, controller)
    due = (NOW + timedelta(hours=9)).isoformat()
    result = await call(executor, "reminders_add", title="Call mom", due=due)
    assert result["success"]
    argv = runner.run.await_args.args
    assert argv[0] == "/usr/bin/osascript"
    assert argv[3:] == ("Call mom", "", "", str(9 * 3600))
    assert "Call mom" not in argv[2]  # Titles are arguments, never script source.
    past = await call(
        executor, "reminders_add", title="x", due=(NOW - timedelta(hours=1)).isoformat()
    )
    assert not past["success"] and "past" in past["error"]


async def test_reminders_are_parsed_sorted_and_rendered():
    runner = AsyncMock()
    runner.run.return_value = (
        "Home\x1fBuy milk\x1f\x1eWork\x1fSend report\x1f2026-10-01T15:00\x1e"
        "Work\x1fTitle, with comma\ttab\x1f2026-10-01T10:00\x1e"
    )
    executor = executor_for(reminders, reminders.RemindersController(runner))
    result = await call(executor, "reminders_list")
    titles = [item["title"] for item in result["result"]["reminders"]]
    assert titles == ["Title, with comma\ttab", "Send report", "Buy milk"]
    assert "due Thu 01 Oct 15:00" in result["display"]


async def test_complete_reminder_matches_key_words_but_refuses_ambiguity():
    listing = "Home\x1fBuy milk\x1f\x1eWork\x1fSend report\x1f\x1eWork\x1fSend invoice\x1f\x1e"
    runner = AsyncMock()
    runner.run.side_effect = [listing, "done"]
    executor = executor_for(reminders, reminders.RemindersController(runner))
    result = await call(executor, "reminders_complete", title="buy the milk")
    assert result["success"] and result["result"]["title"] == "Buy milk"
    assert runner.run.await_args.args[3:] == ("Buy milk", "Home")

    runner.run.side_effect = [listing]
    result = await call(executor, "reminders_complete", title="send")
    assert not result["success"] and "Several open reminders" in result["error"]
    runner.run.side_effect = [listing]
    result = await call(executor, "reminders_complete", title="walk the dog")
    assert not result["success"] and "No open reminder" in result["error"]


# Notes


async def test_note_text_is_escaped_html_and_passed_as_arguments():
    runner = AsyncMock()
    runner.run.return_value = "ok"
    executor = executor_for(notes, notes.NotesController(runner))
    result = await call(
        executor, "notes_create", title="Ideas <b>", body='<script>x</script>\n" & quit'
    )
    assert result["success"]
    argv = runner.run.await_args.args
    body = argv[3]
    assert "&lt;script&gt;" in body and "<script>" not in body
    assert "Ideas" not in argv[2]
    await call(executor, "notes_append", title="Shopping", text="eggs")
    assert runner.run.await_args.args[3:] == ("Shopping", "<div>eggs</div>")


async def test_notes_find_sorts_recent_first_and_read_reports_ambiguity():
    runner = AsyncMock()
    runner.run.return_value = "Old\x1f2025-01-01T10:00\x1eNew\x1f2026-09-30T08:00\x1e"
    executor = executor_for(notes, notes.NotesController(runner))
    result = await call(executor, "notes_find")
    assert [note["title"] for note in result["result"]["notes"]] == ["New", "Old"]
    runner.run.return_value = "many\x1f"
    result = await call(executor, "notes_read", title="Shopping")
    assert not result["success"] and "Several notes" in result["error"]
    runner.run.return_value = "ok\x1feggs\nmilk\n"
    result = await call(executor, "notes_read", title="Shopping")
    assert result["result"]["text"] == "eggs\nmilk"


async def test_unanswered_permission_prompt_explains_what_to_do():
    runner = AsyncMock()
    runner.run.side_effect = TimeoutError
    executor = executor_for(notes, notes.NotesController(runner))
    result = await call(executor, "notes_find")
    assert not result["success"] and "Privacy & Security > Automation" in result["error"]


# Mac Calendar (fake EventKit backend)


class FakeCalendar:
    def __init__(self, events):
        self.items = events
        self.created = []

    def events(self, start, end):
        return [e for e in self.items if start.isoformat() <= e["start"] < end.isoformat()]

    def create(self, title, start, end, calendar, location, notes):
        view = {
            "id": "new",
            "title": title,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "all_day": False,
            "calendar": calendar or "Home",
            "location": location,
            "busy": True,
        }
        self.created.append(view)
        return view

    def delete(self, title, start, end):
        return next(e for e in self.items if e["title"] == title)


def mac_event(title, start, end, busy=True):
    return {
        "id": title,
        "title": title,
        "start": (NOW.replace(hour=start)).isoformat(),
        "end": (NOW.replace(hour=end)).isoformat(),
        "all_day": False,
        "calendar": "Work",
        "location": "",
        "busy": busy,
    }


async def test_mac_calendar_events_free_time_and_create():
    backend = FakeCalendar(
        [mac_event("Standup", 9, 10), mac_event("Lunch", 12, 13), mac_event("Hold", 14, 15, False)]
    )
    executor = executor_for(calendar_mac, calendar_mac.MacCalendarController(backend))
    window = {"start": NOW.replace(hour=8).isoformat(), "end": NOW.replace(hour=17).isoformat()}
    listed = await call(executor, "mac_calendar_events", **window)
    assert "Thu 01 Oct 09:00–10:00 — Standup" in listed["display"]
    free = await call(executor, "mac_calendar_free_time", min_minutes=60, **window)
    assert [slot["start"][11:16] for slot in free["result"]["free"]] == ["08:00", "10:00", "13:00"]
    created = await call(
        executor,
        "mac_calendar_create_event",
        title="Dentist",
        start=NOW.replace(hour=15).isoformat(),
        end=NOW.replace(hour=16).isoformat(),
    )
    assert created["success"] and backend.created[0]["title"] == "Dentist"
    assert "Added “Dentist”" in created["display"]


async def test_mac_calendar_delete_needs_approval():
    backend = FakeCalendar([mac_event("Standup", 9, 10)])
    executor = executor_for(calendar_mac, calendar_mac.MacCalendarController(backend))
    window = {"start": NOW.replace(hour=8).isoformat(), "end": NOW.replace(hour=17).isoformat()}
    pending = await call(executor, "mac_calendar_delete_event", title="Standup", **window)
    assert pending["status"] == "confirmation_required"
    done = await call(executor, "mac_calendar_delete_event", True, title="Standup", **window)
    assert done["success"] and "Deleted “Standup”" in done["display"]


def test_mac_calendar_window_requires_offsets():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        calendar_mac.WindowInput(start="2026-10-01T09:00:00", end="2026-10-01T10:00:00")
