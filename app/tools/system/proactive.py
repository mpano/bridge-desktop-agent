"""Tools for the proactive assistant: watches and heads-up settings."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.workflows.schedules import ScheduleStore
from app.workflows.watches import ProactiveStore


class WatchInput(Input):
    kind: Literal["email", "slack"]
    query: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[^\x00-\x1f\x7f]+$",
        description="Gmail search (e.g. 'from:olivier', 'subject:invoice') or Slack search; "
        "'mentions' watches Slack mentions of the user",
    )
    label: str = Field(
        min_length=1, max_length=80, description="Short name, e.g. 'Emails from Olivier'"
    )


class WatchDeleteInput(Input):
    about: str = Field(min_length=1, max_length=200, description="Words from the watch")


class SettingsInput(Input):
    meeting_prep: bool | None = None
    lead_minutes: int | None = Field(default=None, ge=1, le=60)
    evening_summary: bool | None = None
    evening_time: str | None = Field(default=None, description="24-hour HH:MM")
    morning_plan: bool | None = Field(default=None, description="Weekday 'Plan my day'")
    morning_time: str | None = Field(default=None, description="24-hour HH:MM")
    work_start: str | None = Field(default=None, description="Workday start, 24-hour HH:MM")
    work_end: str | None = Field(default=None, description="Workday end, 24-hour HH:MM")

    @field_validator("evening_time", "morning_time", "work_start", "work_end")
    @classmethod
    def clock(cls, value):
        if value is None:
            return value
        hours, _, minutes = value.partition(":")
        if not (hours.isdigit() and minutes.isdigit() and int(hours) < 24 and int(minutes) < 60):
            raise ValueError("Use a 24-hour HH:MM time.")
        return f"{int(hours):02d}:{int(minutes):02d}"


class ProactiveController:
    def __init__(
        self,
        store: ProactiveStore,
        schedules: ScheduleStore,
        accounts=None,
        clock=lambda: datetime.now().astimezone(),
    ):
        self.store, self.schedules, self.accounts, self.clock = store, schedules, accounts, clock

    async def _require(self, provider: str, name: str) -> None:
        if self.accounts is None:
            raise ValueError(f"Connect {name} in the dashboard's Connections page first.")
        try:
            accounts = (await self.accounts.list_accounts())["accounts"]
        except Exception:
            raise ValueError(f"Connect {name} in the dashboard's Connections page first.") from None
        if not any(item["provider"] == provider for item in accounts):
            raise ValueError(f"Connect {name} in the dashboard's Connections page first.")

    async def watch(self, args):
        await self._require(
            "gmail" if args.kind == "email" else "slack",
            "Gmail" if args.kind == "email" else "Slack",
        )
        watch = self.store.add_watch(args.kind, args.query.strip(), args.label.strip())
        return {"id": watch.id, "kind": watch.kind, "query": watch.query, "label": watch.label}

    async def list(self, _):
        return {
            "watches": [
                {
                    "id": w.id,
                    "kind": w.kind,
                    "query": w.query,
                    "label": w.label,
                    "last_checked": w.last_checked,
                    "last_error": w.last_error,
                }
                for w in self.store.watches()
            ],
            "settings": self.store.settings(),
        }

    async def unwatch(self, args):
        words = args.about.casefold().split()
        matches = [
            w
            for w in self.store.watches()
            if all(word in f"{w.label} {w.query}".casefold() for word in words)
        ]
        if not matches:
            raise ValueError(f"You're not watching anything about “{args.about}”.")
        if len(matches) > 1:
            names = "; ".join(w.label for w in matches[:5])
            raise ValueError(f"Several watches match: {names}. Be more specific.")
        self.store.remove_watch(matches[0].id)
        return {"label": matches[0].label}

    def apply(self, **changes) -> dict:
        """Update settings. The morning brief and evening wrap-up run on their own (see
        app/assistant/briefs.py); older versions kept a scheduled request for each, which
        is removed here."""
        proposed = {**self.store.settings(), **changes}
        if proposed["work_end"] <= proposed["work_start"]:
            raise ValueError("The workday must end after it starts.")
        settings = self.store.update_settings(**changes)
        ids = {}
        for prefix in ("evening", "morning"):
            if settings[f"{prefix}_schedule_id"]:
                self.schedules.delete(settings[f"{prefix}_schedule_id"])
                ids[f"{prefix}_schedule_id"] = 0
        return self.store.update_settings(**ids) if ids else settings

    def upgrade_routines(self) -> None:
        """Once: move to the new brief and wrap-up, and turn the wrap-up on."""
        settings = self.store.settings()
        if settings.get("briefs_v1"):
            return
        self.apply(evening_summary=True)
        self.store.update_settings(briefs_v1=True)

    async def configure(self, args):
        changes = {key: value for key, value in args.model_dump().items() if value is not None}
        return {"settings": self.apply(**changes)}


def describe(settings: dict) -> str:
    meeting = (
        f"Meeting heads-up {settings['lead_minutes']} min before"
        if settings["meeting_prep"]
        else "Meeting heads-ups off"
    )
    evening = (
        f"evening summary weekdays at {settings['evening_time']}"
        if settings["evening_summary"]
        else "evening summary off"
    )
    morning = (
        f"plan my day weekdays at {settings['morning_time']}"
        if settings["morning_plan"]
        else "morning plan off"
    )
    hours = f"workday {settings['work_start']}–{settings['work_end']}"
    return f"{meeting} · {evening} · {morning} · {hours}"


def render_list(data: dict) -> str:
    lines = [describe(data["settings"])]
    if data["watches"]:
        lines.append("Watching:")
        for watch in data["watches"]:
            problem = f" ⚠ {watch['last_error']}" if watch["last_error"] else ""
            lines.append(f"• {watch['label']} ({watch['kind']}: {watch['query']}){problem}")
    else:
        lines.append("Not watching anything. Try “tell me when Olivier emails me”.")
    return "\n".join(lines)


def register(registry, controller: ProactiveController):
    registry.register(
        Tool(
            "watch_create",
            "Notify the user when new matching email or Slack messages arrive, e.g. 'tell me "
            "when Olivier emails me' (kind=email, query='from:olivier') or 'when someone "
            "mentions me on Slack' (kind=slack, query='mentions'). Checked every 2 minutes.",
            WatchInput,
            RiskLevel.SAFE,
            controller.watch,
            render=lambda data: (
                f"✓ I'll let you know: {data['label']}. Checked every 2 minutes "
                "while Bridge is open."
            ),
        )
    )
    registry.register(
        Tool(
            "watch_list",
            "Show what Bridge is watching and the meeting / evening summary settings.",
            Input,
            RiskLevel.SAFE,
            controller.list,
            render=render_list,
        )
    )
    registry.register(
        Tool(
            "watch_delete",
            "Stop one watch, found by words from it, e.g. 'stop watching Olivier'.",
            WatchDeleteInput,
            RiskLevel.SAFE,
            controller.unwatch,
            render=lambda data: f"✓ Stopped: {data['label']}",
        )
    )
    registry.register(
        Tool(
            "proactive_settings",
            "Turn meeting heads-ups on/off or change how many minutes before; turn the weekday "
            "evening summary or the weekday morning 'plan my day' on/off or change their "
            "times; set working hours used for planning.",
            SettingsInput,
            RiskLevel.SAFE,
            controller.configure,
            render=lambda data: "✓ " + describe(data["settings"]),
        )
    )
