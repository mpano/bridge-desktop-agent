"""Tool: let Bridge use the Mac (click, type, use apps) to finish a task, step by step."""

from __future__ import annotations

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class OperateInput(Input):
    task: str = Field(
        min_length=5,
        max_length=500,
        description="What to do in the Mac's apps, in full, e.g. 'In Numbers, export Q3.numbers "
        "as a PDF to the Desktop'",
    )


def register(registry, operator) -> None:
    async def operate(args):
        return operator.start(args.task)

    registry.register(
        Tool(
            "operate_mac",
            "Use the user's Mac like a person would — click, type and use apps — to finish a "
            "task that no other tool can do (apps without a connection, settings inside apps, "
            "exporting files, filling a form). Bridge shows each step on screen and asks before "
            "anything that sends, deletes or buys. Prefer other tools when they can do it.",
            OperateInput,
            RiskLevel.CONFIRM,
            operate,
            confirmation_message="Let Bridge use your Mac to do this?",
            render=lambda d: (
                "Working on it on your Mac — follow along in the panel at the "
                "bottom of the screen. You can stop it anytime."
            ),
        )
    )
