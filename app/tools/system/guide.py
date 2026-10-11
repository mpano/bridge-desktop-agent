"""Tool: "show me how to …" - Bridge guides the user through a task on their Mac."""

from __future__ import annotations

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class GuideInput(Input):
    task: str = Field(
        min_length=5,
        max_length=500,
        description="What the user wants to learn to do, in full, e.g. 'export this Keynote "
        "deck as a PDF' or 'turn on Do Not Disturb'",
    )


def register(registry, guides) -> None:
    async def guide(args):
        return guides.start(args.task)

    registry.register(
        Tool(
            "show_me_how",
            "Guide the user step by step through doing something themselves on their Mac: "
            "Bridge highlights the exact button to click on screen and says what to do, then "
            "waits for them. Use for 'show me how…', 'how do I … in <app>?', 'walk me "
            "through…', 'teach me…'. Use operate_mac instead when they want it done for them.",
            GuideInput,
            RiskLevel.SAFE,
            guide,
            render=lambda d: (
                "Let's do it together — follow the highlight on your screen. "
                "Do each step yourself, or press “Do this step for me”."
            ),
        )
    )
