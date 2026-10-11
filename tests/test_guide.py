"""Show me how: steps from the model shown (not done), noticing you did them, questions,
doing one step for you, handing over, saving to Notes. A fake Mac and a fake model."""

import asyncio
import json

import httpx
import pytest

from app.assistant import guide as guide_module
from app.assistant.guide import Guides, describe, keys_label
from app.assistant.operator import Operator
from app.config.settings import Settings


class FakeMac:
    def __init__(self):
        self.counts = {"clicks": 0, "keys": 0, "scrolls": 0}
        self.done: list = []
        self.last_point = None
        self.front = {"app": "Keynote", "bundle_id": "com.apple.iWork.Keynote"}
        self.pointer = (5.0, 5.0)

    def screenshot(self):
        return b"jpeg", 1440, 900

    def screen_size(self):
        return 1440.0, 900.0

    def frontmost(self):
        return self.front

    def element_at(self, x, y):
        return {
            "role": "AXMenuItem",
            "label": "Export…",
            "shortcut": "⇧⌘E",
            "frame": [90.0, 40.0, 160.0, 22.0],
            **self.front,
        }

    def focused(self):
        return {
            "role": "AXTextField",
            "subrole": "",
            "label": "Save As",
            "frame": [400.0, 300.0, 240.0, 24.0],
            **self.front,
        }

    def activity(self):
        return dict(self.counts)

    def mouse(self):
        return self.pointer

    def keypress(self, keys):
        self.done.append(("keypress", keys))

    def click(self, x, y, button="left", count=1, keys=None):
        self.done.append(("click", x, y))

    def type(self, text):
        self.done.append(("type", text))

    def move(self, x, y, keys=None):
        pass


class FakeHud:
    def __init__(self):
        self.shown: list = []
        self.calls: list = []

    def begin(self):
        self.calls.append("begin")

    def point(self, step):
        self.shown.append(step)

    def clear(self):
        self.calls.append("clear")

    def end(self):
        self.calls.append("end")

    def panel_rect(self):
        return (500.0, 700.0, 460.0, 210.0)


def model(responses):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(seen) - 1])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def call(n, *actions):
    return {
        "id": f"r{n}",
        "output": [{"type": "computer_call", "call_id": f"c{n}", "actions": list(actions)}],
    }


def say(n, text):
    return {
        "id": f"r{n}",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(guide_module, "POLL", 0.01)
    monkeypatch.setattr(guide_module, "TYPING_PAUSE", 0.05)
    settings = Settings(
        _env_file=None, database_path=tmp_path / "db", openai_api_key="sk-x", operator_enabled=True
    )
    mac, hud = FakeMac(), FakeHud()

    async def no_wait(_seconds):
        await asyncio.sleep(0)

    def make(responses, blocked=None):
        http, seen = model(responses)
        operator = Operator(
            tmp_path / "db", settings, hands=mac, sleep=no_wait, blocked=blocked, hud=FakeHud()
        )
        guides = Guides(
            tmp_path / "db",
            settings,
            hands=mac,
            http=http,
            operator=operator,
            hud=hud,
            sleep=no_wait,
            blocked=blocked,
        )
        return guides, seen

    return mac, hud, make


async def until(guides, status, number=None):
    for _ in range(500):
        current = guides.current.view()
        if current["status"] == status and (number is None or current["number"] == number):
            return current
        await asyncio.sleep(0.01)
    raise AssertionError(f"Still {guides.current.view()['status']}")


async def test_a_whole_guide(setup):
    mac, hud, make = setup
    guides, seen = make(
        [
            call(1, {"type": "click", "x": 120, "y": 50}),
            call(2, {"type": "type", "text": "report.pdf"}),
            say(3, "Which folder should it go in?"),
            call(4, {"type": "keypress", "keys": ["ENTER"]}),
            say(5, "Done — your PDF is on the Desktop."),
        ]
    )
    guides.executor = Notes()
    started = guides.start("export this deck as a PDF")
    first = await until(guides, "waiting", 1)
    assert first["step"]["text"] == "Choose **Export…**"
    assert first["step"]["tip"] == "Next time: ⇧⌘E"
    assert hud.shown[0]["frame"] == [90.0, 40.0, 160.0, 22.0] and hud.calls[0] == "begin"
    assert mac.done == []  # Shown, never done for you.
    assert "Show me how to: export this deck" in seen[0]["input"][0]["content"][0]["text"]

    mac.pointer = (600.0, 750.0)  # A click on Bridge's own panel doesn't count.
    mac.counts["clicks"] += 1
    await asyncio.sleep(0.05)
    assert guides.current.view()["number"] == 1
    mac.pointer = (120.0, 50.0)
    mac.counts["clicks"] += 1
    second = await until(guides, "waiting", 2)
    assert second["step"]["text"] == "Type `report.pdf` in the **Save As** field"
    assert seen[1]["previous_response_id"] == "r1"
    assert seen[1]["input"][0]["type"] == "computer_call_output"

    mac.counts["keys"] += 10  # You type, then pause.
    asking = await until(guides, "asking")
    assert asking["note"] == "Which folder should it go in?"
    guides.command(started["id"], "answer", "The Desktop")
    third = await until(guides, "waiting", 3)
    assert seen[3]["input"] == [{"role": "user", "content": "The Desktop"}]
    assert third["step"]["text"] == "Press **Return**"

    guides.command(started["id"], "do")  # Do this step for me.
    done = await until(guides, "done")
    assert mac.done == [("keypress", ["ENTER"])]
    assert done["result"] == "Done — your PDF is on the Desktop."
    assert hud.calls[-1] == "end"

    saved = await guides.save(started["id"])
    assert saved["saved"] is True
    title, body = guides.executor.notes[0]
    assert title == "How to: export this deck as a PDF"
    assert body.startswith("1. Choose Export… (Next time: ⇧⌘E)\n2. Type report.pdf in the Save As")
    assert guides.recent()[0]["steps"] == 3


class Notes:
    def __init__(self):
        self.notes = []

    async def execute(self, tool_call, request_id, approved=False):
        assert tool_call.name == "notes_create"
        self.notes.append((tool_call.arguments["title"], tool_call.arguments["body"]))
        return {"status": "completed"}


async def test_stop_hand_over_and_blocked_apps(setup):
    mac, hud, make = setup
    guides, _ = make([call(1, {"type": "click", "x": 10, "y": 10})])
    started = guides.start("turn on dark mode")
    await until(guides, "waiting", 1)
    guides.command(started["id"], "stop")
    stopped = await until(guides, "stopped")
    assert stopped["result"] == "Guide stopped." and hud.calls[-1] == "end"
    with pytest.raises(Exception, match="finished"):
        guides.command(started["id"], "next")

    guides, _ = make([call(1, {"type": "click", "x": 10, "y": 10})])
    started = guides.start("turn on dark mode")
    await until(guides, "waiting", 1)
    handed = []
    guides.operator.start = handed.append
    guides.command(started["id"], "rest")
    await until(guides, "done")
    assert handed[0].startswith("turn on dark mode (The user has done part of it already")

    guides, _ = make(
        [call(1, {"type": "click", "x": 10, "y": 10})],
        blocked=lambda app, bundle: app == "Keynote",
    )
    guides.start("open my passwords")
    failed = await until(guides, "failed")
    assert "doesn't look at Keynote" in failed["result"]


async def test_needs_the_setting_and_one_at_a_time(setup, tmp_path):
    mac, hud, make = setup
    guides, _ = make([call(1, {"type": "click", "x": 10, "y": 10})])
    guides.settings.operator_enabled = False
    with pytest.raises(Exception, match="Use your Mac"):
        guides.start("anything at all")
    guides.settings.operator_enabled = True
    guides.start("first task here")
    with pytest.raises(Exception, match="already running"):
        guides.start("second task here")
    guides.command(guides.current.id, "stop")
    await until(guides, "stopped")


def test_steps_in_words():
    menu_bar = {"role": "AXMenuBarItem", "label": "File", "app": "Keynote"}
    assert describe({"type": "click"}, menu_bar, {})[0] == "Open the **File** menu"
    button = {"role": "AXButton", "label": "Save", "app": "Pages"}
    assert describe({"type": "click"}, button, {})[0] == "Click **Save** in Pages"
    assert describe({"type": "double_click"}, {"role": "AXCell", "label": "a.pdf"}, {})[0] == (
        "Double-click **a.pdf**"
    )
    assert describe({"type": "click"}, {"role": "AXDockItem", "label": "Notes"}, {})[0] == (
        "Click **Notes** in the Dock"
    )
    assert describe({"type": "click"}, {}, {})[0] == "Click the highlighted spot"
    secure = {"role": "AXSecureTextField", "label": "Password"}
    assert "Bridge doesn't see it" in describe({"type": "type", "text": "x"}, {}, secure)[0]
    assert describe({"type": "keypress", "keys": ["CMD", "SPACE"]}, {}, {})[0] == (
        "Press **⌘ Space** to open Spotlight"
    )
    assert keys_label(["cmd", "shift", "e"]) == "⌘⇧E"
    assert describe({"type": "scroll", "scroll_y": -300}, {}, {})[0] == "Scroll up"


def test_guide_routes(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.bootstrap import build_agent

    monkeypatch.chdir(tmp_path)
    config = Settings(
        _env_file=None, database_path=tmp_path / "db", api_token="token", openai_api_key="sk-x"
    )
    agent = build_agent(config, llm=AsyncMock())
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(config, agent, enable_ui=True)) as client:
        overview = client.get("/api/v1/guide", headers=headers).json()
        assert overview["current"] is None and overview["available"] is False
        refused = client.post(
            "/api/v1/guide/start", json={"task": "show me dark mode"}, headers=headers
        )
        assert refused.status_code == 409 and "Use your Mac" in refused.json()["detail"]
        assert (
            client.post(
                "/api/v1/guide/command", json={"id": "abcdef12", "what": "next"}, headers=headers
            ).status_code
            == 409
        )
        assert "BridgeGuide" in client.get("/ui/guide.js").text
        assert "guide.js" in client.get("/ui/guide.html").text
        assert client.get("/api/v1/guide").status_code == 401
    assert "show_me_how" in [t.name for t in agent.executor.registry.tools()]
