"""Immediate local notifications; scheduling and delivery receipts are not supported."""

from pydantic import Field, field_validator

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.macos.applescript import MacOSAppleScript
from app.tools.registry import ToolRegistry


class NotificationInput(Input):
    title: str = Field(default="Desktop Agent", min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1000)

    @field_validator("title", "message")
    @classmethod
    def plain_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Notification text must not be blank.")
        MacOSAppleScript.literal(value)
        return value


class NotificationController:
    def __init__(self, script: MacOSAppleScript):
        self.script = script

    async def show(self, args: NotificationInput) -> dict:
        await self.script.run(
            f"display notification {self.script.literal(args.message)} "
            f"with title {self.script.literal(args.title)}"
        )
        return {
            "accepted": True,
            "delivery_verified": False,
            "message": "macOS accepted the notification request. Visible delivery is not verified.",
        }


def register(registry: ToolRegistry, controller: NotificationController) -> None:
    registry.register(
        Tool(
            "show_notification",
            "Request an immediate local macOS notification after approval. "
            "No scheduling, delivery receipt, or automatic completion alerts. "
            "Use only when the user asks for a notification.",
            NotificationInput,
            RiskLevel.CONFIRM,
            controller.show,
            confirmation_message="Display this notification now? Its text may be visible "
            "to others on your screen or lock screen, depending on your macOS settings.",
        )
    )


NOTIFY_SCRIPT = """
on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
end run
"""


async def post_notification(runner, title: str, message: str) -> None:
    """Bridge's own status alerts (scheduled results, approvals). Text is passed as
    arguments, so it can never become script source. Failures are ignored."""
    clean = lambda text, limit: " ".join(text.split())[:limit]  # noqa: E731
    try:
        await runner.run(
            "/usr/bin/osascript", "-e", NOTIFY_SCRIPT, clean(title, 100), clean(message, 250)
        )
    except Exception:
        pass
