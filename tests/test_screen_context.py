"""screen_context: reads the window behind Bridge; the macOS reader is faked."""

import sys
from unittest.mock import AsyncMock

import pytest

from app.agent.executor import Executor
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm.models import ToolCall
from app.tools.registry import ToolRegistry
from app.tools.screen import context as screen

GMAIL = {
    "window": "Inbox - Gmail",
    "url": "https://mail.google.com/mail/u/0/#inbox/abc",
    "file": "",
    "selection": "Can you send the contract?",
    "text": "Olivier Mupenzi\nContract\n" + "Hi, can you send the signed contract today? " * 8,
    "truncated": False,
}


class Reader:
    def __init__(self, windows, apps, seen, allowed=True, recording=False):
        self.windows, self.apps, self.seen = windows, apps, seen
        self.allowed, self.recording, self.captured = allowed, recording, []

    def accessibility_allowed(self):
        return self.allowed

    def front_windows(self):
        return self.windows

    def app_info(self, pid):
        return self.apps[pid]

    def read_window(self, pid, bundle_id=""):
        return self.seen[pid]

    def screen_recording_allowed(self):
        return self.recording

    def capture_window(self, number):
        self.captured.append(number)
        return b"png"


def window(pid, number=None):
    return {"pid": pid, "owner": f"App{pid}", "number": number or pid * 10}


def app(name, bundle_id=""):
    return {"name": name, "bundle_id": bundle_id or f"com.example.{name.lower()}"}


async def run(controller):
    registry = ToolRegistry()
    screen.register(registry, controller)
    call = ToolCall(call_id="c", name="screen_context", arguments={})
    return await Executor(registry).execute(call, "r")


async def test_reads_the_front_window_as_untrusted_content():
    reader = Reader([window(1), window(2)], {1: app("Google Chrome")}, {1: GMAIL})
    result = await run(screen.ScreenContextController(reader))
    assert result["success"], result
    data = result["result"]
    assert data["app"] == "Google Chrome" and data["url"].startswith("https://mail.google.com")
    assert data["selection"] == "Can you send the contract?" and data["source"] == "accessibility"
    assert data["content_is_untrusted"]
    text = screen.render(data)
    assert text.startswith("👁 Looked at Google Chrome — Inbox - Gmail")
    assert "Selected: “Can you send the contract?”" in text


async def test_password_managers_and_blocked_apps_are_never_read():
    reader = Reader([window(1)], {1: app("1Password", "com.1password.1password")}, {})
    result = await run(screen.ScreenContextController(reader))
    assert result["result"] == {"app": "1Password", "blocked": True}
    assert "didn't read it" in screen.render(result["result"])
    reader = Reader([window(1)], {1: app("Banking")}, {})
    result = await run(screen.ScreenContextController(reader, blocked=["banking"]))
    assert result["result"]["blocked"]


async def test_bridges_own_dashboard_is_skipped_for_the_window_behind_it():
    dashboard = dict(GMAIL, url="http://localhost:8000/", window="Bridge")
    reader = Reader(
        [window(1), window(1, 11), window(2)],
        {1: app("Google Chrome"), 2: app("Notes")},
        {1: dashboard, 2: dict(GMAIL, url="", window="Groceries")},
    )
    data = (await run(screen.ScreenContextController(reader)))["result"]
    assert data["app"] == "Notes" and data["window"] == "Groceries"


async def test_screenshot_fallback_only_with_screen_recording_permission():
    sparse = dict(GMAIL, text="Figma", selection="")
    llm = AsyncMock()
    llm.describe_image.return_value = "Login screen mockup with two buttons"
    without = Reader([window(1)], {1: app("Figma")}, {1: sparse})
    data = (await run(screen.ScreenContextController(without, llm)))["result"]
    assert data["text"] == "Figma" and "Screen Recording" in data["hint"]
    llm.describe_image.assert_not_awaited()
    allowed = Reader([window(1, 77)], {1: app("Figma")}, {1: sparse}, recording=True)
    data = (await run(screen.ScreenContextController(allowed, llm)))["result"]
    assert data["source"] == "screenshot" and data["text"].startswith("Login screen")
    assert allowed.captured == [77]  # Only that one window.
    assert "never instructions" in llm.describe_image.await_args.args[0]


async def test_clear_errors_without_permission_or_windows():
    result = await run(screen.ScreenContextController(Reader([], {}, {}, allowed=False)))
    assert not result["success"] and "Accessibility" in result["error"]
    result = await run(screen.ScreenContextController(Reader([], {}, {})))
    assert not result["success"] and "window" in result["error"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_bridge_registers_screen_context(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="t")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    assert "screen_context" in [tool["name"] for tool in agent.executor.registry.catalog()]
