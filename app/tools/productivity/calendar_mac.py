"""The Mac's Calendar (iCloud, Google, Exchange… as added in System Settings) via EventKit.

EventKit expands repeating events, which Calendar's AppleScript does not. The backend is
injectable so tests never touch real calendars.
"""

import asyncio
import threading
from datetime import datetime, timedelta

from pydantic import Field, field_validator, model_validator

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.productivity.timeslots import free_slots

DENIED = (
    "Bridge can't access Calendar. Allow it in System Settings > Privacy & Security > "
    "Calendars (choose Full Access), then try again."
)


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class WindowInput(Input):
    start: str = Field(max_length=40, description="ISO 8601 date-time with UTC offset")
    end: str = Field(max_length=40, description="ISO 8601 date-time with UTC offset")

    @field_validator("start", "end")
    @classmethod
    def offset_required(cls, value):
        if "T" not in value or parse(value).utcoffset() is None:
            raise ValueError("Use a full date-time with UTC offset.")
        return value

    @model_validator(mode="after")
    def valid_window(self):
        start, end = parse(self.start), parse(self.end)
        if end <= start or end - start > timedelta(days=90):
            raise ValueError("Choose an increasing time window of at most 90 days.")
        return self


class FreeTimeInput(WindowInput):
    min_minutes: int = Field(default=30, ge=5, le=480)


class CreateInput(WindowInput):
    title: str = Field(min_length=1, max_length=300, pattern=r"^[^\x00-\x1f\x7f]+$")
    calendar: str | None = Field(default=None, max_length=200, description="Omit for default")
    location: str = Field(default="", max_length=300)
    notes: str = Field(default="", max_length=5000)


class DeleteInput(WindowInput):
    title: str = Field(min_length=1, max_length=300, description="Exact event title")


class EventKitBackend:
    """Blocking EventKit calls; the controller runs them off the event loop."""

    def __init__(self):
        self._guard = threading.Lock()
        self._store = None

    def _ready_store(self):
        import EventKit as EK

        status = EK.EKEventStore.authorizationStatusForEntityType_(EK.EKEntityTypeEvent)
        if self._store is None:
            self._store = EK.EKEventStore.alloc().init()
        if status == EK.EKAuthorizationStatusNotDetermined:
            answered, outcome = threading.Event(), {}

            def handler(granted, _error):
                outcome["granted"] = bool(granted)
                answered.set()

            self._store.requestFullAccessToEventsWithCompletion_(handler)
            if not answered.wait(120) or not outcome.get("granted"):
                raise ValueError(DENIED)
            self._store = EK.EKEventStore.alloc().init()
        elif status != EK.EKAuthorizationStatusFullAccess:
            raise ValueError(DENIED)
        return self._store

    @staticmethod
    def _date(value: datetime):
        from Foundation import NSDate

        return NSDate.dateWithTimeIntervalSince1970_(value.timestamp())

    @staticmethod
    def _python(value) -> datetime:
        return datetime.fromtimestamp(value.timeIntervalSince1970()).astimezone()

    def _view(self, event) -> dict:
        return {
            "id": str(event.eventIdentifier() or ""),
            "title": str(event.title() or ""),
            "start": self._python(event.startDate()).isoformat(),
            "end": self._python(event.endDate()).isoformat(),
            "all_day": bool(event.isAllDay()),
            "calendar": str(event.calendar().title() or ""),
            "location": str(event.location() or ""),
            # Free/tentative-as-free events don't block time (EKEventAvailabilityFree == 1).
            "busy": event.availability() != 1,
        }

    def _find(self, store, start: datetime, end: datetime):
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
            self._date(start), self._date(end), None
        )
        events = store.eventsMatchingPredicate_(predicate) or []
        return sorted(events, key=lambda event: event.startDate().timeIntervalSince1970())

    def events(self, start: datetime, end: datetime) -> list[dict]:
        with self._guard:
            store = self._ready_store()
            return [self._view(event) for event in self._find(store, start, end)]

    def create(self, title, start, end, calendar, location, notes) -> dict:
        import EventKit as EK

        with self._guard:
            store = self._ready_store()
            target = store.defaultCalendarForNewEvents()
            if calendar:
                matches = [
                    item
                    for item in store.calendarsForEntityType_(EK.EKEntityTypeEvent)
                    if str(item.title()).casefold() == calendar.casefold()
                    and item.allowsContentModifications()
                ]
                if len(matches) != 1:
                    raise ValueError(f"No single writable calendar named “{calendar}”.")
                target = matches[0]
            if target is None:
                raise ValueError("No default calendar is set. Name a calendar to use.")
            event = EK.EKEvent.eventWithEventStore_(store)
            event.setTitle_(title)
            event.setStartDate_(self._date(start))
            event.setEndDate_(self._date(end))
            event.setCalendar_(target)
            if location:
                event.setLocation_(location)
            if notes:
                event.setNotes_(notes)
            saved, error = store.saveEvent_span_commit_error_(event, EK.EKSpanThisEvent, True, None)
            if not saved:
                raise ValueError("Calendar did not save the event. Check the calendar is writable.")
            return self._view(event)

    def delete(self, title, start, end) -> dict:
        import EventKit as EK

        with self._guard:
            store = self._ready_store()
            wanted = title.strip().casefold()
            matches = [
                event
                for event in self._find(store, start, end)
                if str(event.title() or "").strip().casefold() == wanted
            ]
            if not matches:
                raise ValueError(f"No event titled “{title}” was found in that time window.")
            if len(matches) > 1:
                raise ValueError(
                    f"{len(matches)} events titled “{title}” are in that window. Narrow the time."
                )
            view = self._view(matches[0])
            # Only this occurrence of a repeating event is removed.
            removed, error = store.removeEvent_span_commit_error_(
                matches[0], EK.EKSpanThisEvent, True, None
            )
            if not removed:
                raise ValueError("Calendar did not delete the event. It may be read-only.")
            return view


class MacCalendarController:
    def __init__(self, backend=None):
        self.backend = backend or EventKitBackend()

    async def events(self, args):
        items = await asyncio.to_thread(self.backend.events, parse(args.start), parse(args.end))
        return {"events": items[:100], "has_more": len(items) > 100}

    async def free_time(self, args):
        start, end = parse(args.start), parse(args.end)
        items = await asyncio.to_thread(self.backend.events, start, end)
        busy = [(parse(item["start"]), parse(item["end"])) for item in items if item["busy"]]
        slots = free_slots(busy, start, end, args.min_minutes)
        return {"free": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in slots]}

    async def create(self, args):
        view = await asyncio.to_thread(
            self.backend.create,
            args.title,
            parse(args.start),
            parse(args.end),
            args.calendar,
            args.location,
            args.notes,
        )
        return {**view, "message": f"Added “{args.title}” to {view['calendar']}."}

    async def delete(self, args):
        view = await asyncio.to_thread(
            self.backend.delete, args.title, parse(args.start), parse(args.end)
        )
        return {**view, "message": f"Deleted “{view['title']}” from {view['calendar']}."}


def _when(item: dict) -> str:
    start = parse(item["start"])
    if item.get("all_day"):
        return start.strftime("%a %d %b") + " (all day)"
    return start.strftime("%a %d %b %H:%M") + "–" + parse(item["end"]).strftime("%H:%M")


def render_events(data: dict) -> str:
    if not data["events"]:
        return "Nothing on your calendar then."
    rows = []
    for item in data["events"]:
        line = f"• {_when(item)} — {item['title'] or '(no title)'}"
        if item.get("location"):
            line += f" @ {item['location']}"
        rows.append(line)
    return "Calendar:\n" + "\n".join(rows)


def render_free(data: dict) -> str:
    if not data["free"]:
        return "No free time in that window."
    rows = [
        f"• {parse(item['start']).strftime('%a %d %b %H:%M')} → "
        f"{parse(item['end']).strftime('%H:%M')}"
        for item in data["free"]
    ]
    return "Free time:\n" + "\n".join(rows)


def register(registry, controller):
    specs = [
        (
            "mac_calendar_events",
            "List events from every calendar on this Mac (iCloud, Google, Exchange… as set up "
            "in System Settings) in a time window, including repeating events.",
            WindowInput,
            controller.events,
            RiskLevel.SAFE,
            render_events,
        ),
        (
            "mac_calendar_free_time",
            "Find free gaps between events on this Mac's calendars in a window.",
            FreeTimeInput,
            controller.free_time,
            RiskLevel.SAFE,
            render_free,
        ),
        (
            "mac_calendar_create_event",
            "Add an event to the Mac's default calendar (or a named one). No invitations are sent.",
            CreateInput,
            controller.create,
            RiskLevel.SAFE,
            lambda data: "✓ " + data["message"] + " " + _when(data),
        ),
        (
            "mac_calendar_delete_event",
            "Delete the one event with this exact title in the window, after approval. For "
            "repeating events only that occurrence is removed.",
            DeleteInput,
            controller.delete,
            RiskLevel.CONFIRM,
            lambda data: "✓ " + data["message"],
        ),
    ]
    for name, description, schema, handler, risk, renderer in specs:
        registry.register(
            Tool(
                name,
                description,
                schema,
                risk,
                handler,
                render=renderer,
                confirmation_message="Delete this calendar event?"
                if risk == RiskLevel.CONFIRM
                else None,
            )
        )
