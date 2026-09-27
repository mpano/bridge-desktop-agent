from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.agent.executor import Executor
from app.llm.models import ToolCall
from app.tools.macos.applescript import MacOSAppleScript
from app.tools.registry import ToolRegistry
from app.tools.system.notifications import NotificationController, NotificationInput, register


async def test_notification_confirmation_and_native_request():
    runner = AsyncMock()
    registry = ToolRegistry()
    register(registry, NotificationController(MacOSAppleScript(runner)))
    executor = Executor(registry)
    call = ToolCall(call_id="1", name="show_notification", arguments={"message": "Take a break"})
    assert (await executor.execute(call, "test"))["status"] == "confirmation_required"
    runner.run.assert_not_called()
    result = await executor.execute(call, "test", approved=True)
    assert result["success"]
    assert result["result"]["delivery_verified"] is False
    runner.run.assert_awaited_once_with(
        "/usr/bin/osascript",
        "-e",
        'display notification "Take a break" with title "Desktop Agent"',
    )
    assert not registry.get("show_notification").persist_arguments
    runner.run.side_effect = RuntimeError("macOS denied permission")
    assert not (await executor.execute(call, "test", approved=True))["success"]


async def test_notification_escapes_script_syntax():
    runner = AsyncMock()
    script = MacOSAppleScript(runner)
    text = '" & do shell script "whoami" & " \\ 🌍'
    await NotificationController(script).show(NotificationInput(message=text))
    assert runner.run.call_args.args[-1] == (
        f"display notification {script.literal(text)} with title {script.literal('Desktop Agent')}"
    )


@pytest.mark.parametrize(
    "values",
    [
        {"message": ""},
        {"message": " "},
        {"message": "a\nb"},
        {"message": "x" * 1001},
        {"message": "ok", "title": "x" * 101},
        {"message": "ok", "delay_seconds": 60},
    ],
)
def test_notification_rejects_invalid_or_scheduled_requests(values):
    with pytest.raises(ValidationError):
        NotificationInput(**values)
