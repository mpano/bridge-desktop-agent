"""Reminders for a moment as a tool: "remind me to … when I open Figma / when the standup
starts / when I'm back"."""

from __future__ import annotations

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class MomentInput(Input):
    text: str = Field(
        min_length=5,
        max_length=400,
        description="The user's words, e.g. 'remind me to ask about the budget when the "
        "standup starts'",
    )


def register(registry, moments) -> None:
    async def remind(args):
        return await moments.add(args.text)

    registry.register(
        Tool(
            "moment_reminder",
            "A reminder for a moment instead of a time: when the user opens an app ('when I "
            "open Figma'), when a calendar meeting starts ('when the standup starts', 'when I "
            "join the design review'), or when they're back at their Mac after being away. "
            "Pass the user's words. For reminders at a time, use the Reminders tools instead.",
            MomentInput,
            RiskLevel.SAFE,
            remind,
            render=lambda d: f"✓ {d['when']}, I'll remind you: {d['text']}.",
        )
    )
