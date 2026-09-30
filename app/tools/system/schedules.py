"""Tools to create, list and delete scheduled requests."""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.workflows.schedules import WEEKDAYS, ScheduleStore


class ScheduleCreateInput(Input):
    message: str = Field(
        min_length=3,
        max_length=500,
        description="The request to run, phrased as the user would ask it, e.g. 'brief me'",
    )
    repeat: Literal["once", "daily", "weekdays", "weekly"]
    time: str = Field(
        max_length=40,
        description="HH:MM (24h local) for repeating schedules; ISO date-time with offset for once",
    )
    weekday: Literal[tuple(WEEKDAYS)] | None = Field(
        default=None, description="Required for weekly"
    )

    @model_validator(mode="after")
    def consistent(self):
        if self.repeat == "once":
            moment = datetime.fromisoformat(self.time.replace("Z", "+00:00"))
            if moment.utcoffset() is None:
                raise ValueError("A one-time schedule needs a date-time with UTC offset.")
        else:
            hours, _, minutes = self.time.partition(":")
            if not (
                hours.isdigit() and minutes.isdigit() and int(hours) < 24 and int(minutes) < 60
            ):
                raise ValueError("Repeating schedules use a 24-hour HH:MM time.")
            self.time = f"{int(hours):02d}:{int(minutes):02d}"
        if (self.repeat == "weekly") != (self.weekday is not None):
            raise ValueError("Give a weekday for weekly schedules only.")
        return self


class ScheduleDeleteInput(Input):
    schedule_id: int | None = Field(default=None, ge=1)
    matching: str | None = Field(
        default=None, max_length=200, description="Words from the scheduled request"
    )

    @model_validator(mode="after")
    def one_target(self):
        if (self.schedule_id is None) == (self.matching is None):
            raise ValueError("Give either schedule_id or matching.")
        return self


class ScheduleController:
    def __init__(self, store: ScheduleStore, clock=lambda: datetime.now().astimezone()):
        self.store, self.clock = store, clock

    @staticmethod
    def _view(item) -> dict:
        return {
            "id": item.id,
            "message": item.message,
            "when": item.describe(),
            "next_run": item.next_run,
            "last_status": item.last_status,
        }

    async def create(self, args):
        weekday = WEEKDAYS.index(args.weekday) if args.weekday else None
        time = args.time.replace("Z", "+00:00") if args.repeat == "once" else args.time
        item = self.store.add(args.message, args.repeat, time, weekday, self.clock())
        return self._view(item)

    async def list(self, _):
        return {"schedules": [self._view(item) for item in self.store.list()]}

    async def delete(self, args):
        items = self.store.list()
        if args.schedule_id is not None:
            matches = [item for item in items if item.id == args.schedule_id]
        else:
            wanted = args.matching.casefold()
            matches = [item for item in items if wanted in item.message.casefold()]
        if not matches:
            raise ValueError("No scheduled task matches that.")
        if len(matches) > 1:
            raise ValueError(f"{len(matches)} scheduled tasks match. Be more specific.")
        self.store.delete(matches[0].id)
        return {"deleted": self._view(matches[0])}


def _next(value: str | None) -> str:
    if not value:
        return "finished"
    return "next " + datetime.fromisoformat(value).strftime("%a %d %b %H:%M")


def render_list(data: dict) -> str:
    if not data["schedules"]:
        return "No scheduled tasks."
    rows = [
        f"• #{item['id']} “{item['message']}” — {item['when']} ({_next(item['next_run'])})"
        for item in data["schedules"]
    ]
    return "Scheduled tasks:\n" + "\n".join(rows)


def register(registry, controller):
    registry.register(
        Tool(
            "schedule_create",
            "Schedule a request to run automatically, e.g. 'every weekday at 08:30 brief me' "
            "or once at a date-time. Runs while Bridge is open; results arrive as a "
            "notification. For simple alerts prefer reminders_add.",
            ScheduleCreateInput,
            RiskLevel.CONFIRM,
            controller.create,
            confirmation_message="Create this automatic scheduled request? Actions it needs "
            "that require approval will still ask you each time.",
            render=lambda data: (
                f"✓ Scheduled “{data['message']}” {data['when']} ({_next(data['next_run'])})."
            ),
        )
    )
    registry.register(
        Tool(
            "schedule_list",
            "List scheduled requests with their timing and next run.",
            Input,
            RiskLevel.SAFE,
            controller.list,
            render=render_list,
        )
    )
    registry.register(
        Tool(
            "schedule_delete",
            "Delete one scheduled request by its number or by words from it.",
            ScheduleDeleteInput,
            RiskLevel.SAFE,
            controller.delete,
            render=lambda data: f"✓ Deleted the schedule “{data['deleted']['message']}”.",
        )
    )
