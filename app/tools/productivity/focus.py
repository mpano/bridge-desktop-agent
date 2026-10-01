"""Tools: focus mode (start, stop, status)."""

from __future__ import annotations

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class FocusStartInput(Input):
    minutes: int = Field(default=60, ge=10, le=240, description="How long to focus")
    task: str = Field(default="", max_length=80, description="What the user will work on")
    calendar: bool = Field(default=True, description="Block the time on the calendar")
    slack: bool = Field(default=True, description="Set Slack status and pause notifications")
    music: bool = Field(default=True, description="Play a focus playlist on Spotify")
    playlist: str | None = Field(
        default=None, max_length=100, description="Spotify playlist to play, if the user named one"
    )


class FocusController:
    def __init__(self, focus):
        self.focus = focus

    async def start(self, args):
        return await self.focus.start(
            args.minutes,
            args.task,
            calendar=args.calendar,
            slack=args.slack,
            music=args.music,
            playlist=args.playlist,
        )

    async def stop(self, _):
        return await self.focus.stop()

    async def status(self, _):
        return self.focus.status()


def render_start(data: dict) -> str:
    about = f" on {data['task']}" if data["task"] else ""
    lines = [f"🎯 Focusing{about} until {data['until']} ({data['minutes']} min)."]
    lines += [f"✓ {item}" for item in data["done"]]
    lines += [f"– {item}" for item in data["skipped"]]
    lines.append("Say “stop focusing” to end early.")
    return "\n".join(lines)


def render_stop(data: dict) -> str:
    if data.get("status") == "already_ended":
        return "Focus had already ended."
    about = f" on {data['task']}" if data["task"] else ""
    ended = "done" if data["status"] == "done" else "stopped"
    head = f"✓ Focus {ended}: {data['minutes']} min{about}."
    return head + "\n" + (data["summary"] or "Nothing new came in while you focused.")


def render_status(data: dict) -> str:
    if not data["active"]:
        return "You're not in focus mode."
    about = f" on {data['task']}" if data["task"] else ""
    return f"🎯 Focusing{about} until {data['until']} ({data['minutes_left']} min left)."


def register(registry, controller: FocusController):
    registry.register(
        Tool(
            "focus_start",
            "Start focus mode for a number of minutes: blocks the calendar, sets the user's "
            "Slack status with Do Not Disturb, and plays focus music. Everything is undone "
            "at the end, and the user is told what they missed.",
            FocusStartInput,
            RiskLevel.CONFIRM,
            controller.start,
            render=render_start,
            confirmation_message="Start focus mode?",
        )
    )
    registry.register(
        Tool(
            "focus_stop",
            "End focus mode now and summarize what the user missed.",
            Input,
            RiskLevel.SAFE,
            controller.stop,
            render=render_stop,
        )
    )
    registry.register(
        Tool(
            "focus_status",
            "Whether focus mode is on and how long is left.",
            Input,
            RiskLevel.SAFE,
            controller.status,
            render=render_status,
        )
    )
