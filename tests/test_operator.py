"""Bridge using the Mac: the steps run, and every safety rule holds."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.server import create_app
from app.assistant.operator import MAX_ACTIONS, Operator, OperatorError
from app.bootstrap import BRIDGE_BUNDLE, build_agent
from app.config.settings import Settings
from app.phone.access import MAC_ONLY_TOOLS
from app.security.risk import RiskLevel
from app.tools.screen.context import is_blocked


class FakeMac:
    """Records what it would do; never touches the real screen, mouse or keyboard."""

    def __init__(self, elements=None, focused=None, front=("Numbers", "com.apple.iWork.Numbers")):
        self.done = []
        self.elements = elements or {}
        self.field = focused or {
            "role": "AXTextField",
            "subrole": "",
            "app": "Numbers",
            "bundle_id": "com.apple.iWork.Numbers",
        }
        self.front = {"app": front[0], "bundle_id": front[1]}
        self.last_point = None
        self.pointer = None

    def screen_size(self):
        return 1440.0, 900.0

    def screenshot(self):
        return b"jpeg", 720, 450  # Half size: the model's (x, y) are doubled on the screen.

    def frontmost(self):
        return self.front

    def element_at(self, x, y):
        return self.elements.get(
            (x, y),
            {
                "role": "AXButton",
                "label": "Export",
                "app": "Numbers",
                "bundle_id": "com.apple.iWork.Numbers",
            },
        )

    def focused(self):
        return self.field

    def mouse(self):
        return self.pointer or self.last_point or (0, 0)

    def click(self, x, y, button="left", count=1, keys=None):
        self.done.append(("click", x, y, button, count))
        self.last_point = (x, y)

    def type(self, text):
        self.done.append(("type", text))

    def keypress(self, keys):
        self.done.append(("keys", tuple(keys)))

    def scroll(self, x, y, dx, dy, keys=None):
        self.done.append(("scroll", x, y, dx, dy))

    def move(self, x, y, keys=None):
        self.last_point = (x, y)

    def drag(self, path, keys=None):
        self.done.append(("drag", tuple(path)))


class Model:
    """A computer-use model: a script of turns, each a list of actions (or a final sentence)."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        turn = self.turns.pop(0) if self.turns else "Done."
        n = len(self.requests)
        if isinstance(turn, str):
            output = [{"type": "message", "content": [{"type": "output_text", "text": turn}]}]
        else:
            output = [
                {
                    "type": "computer_call",
                    "call_id": f"call-{n}",
                    "actions": turn,
                    "status": "completed",
                }
            ]
        return httpx.Response(200, json={"id": f"resp-{n}", "output": output})


def operator(tmp_path, mac, model, **settings):
    config = SimpleNamespace(
        **{
            "operator_enabled": True,
            "operator_ask": "important",
            "operator_model": "gpt-6.1-sol",
            "openai_api_key": SecretStr("sk-test"),
            "screen_context_blocked_apps": [],
            **settings,
        }
    )
    notes = []

    async def notify(title, body, view):
        notes.append(title)

    async def no_wait(seconds):
        await asyncio.sleep(0)

    op = Operator(
        tmp_path / "db",
        config,
        hands=mac,
        http=httpx.AsyncClient(transport=httpx.MockTransport(model)),
        blocked=lambda app, bundle: is_blocked(app, bundle, [BRIDGE_BUNDLE]),
        notify=notify,
        sleep=no_wait,
    )
    op.notes = notes
    return op


async def until(op, *statuses):
    for _ in range(500):
        if op.current and op.current.status in statuses:
            return op.current
        await asyncio.sleep(0)
    raise AssertionError(f"never reached {statuses}: {op.current.status if op.current else None}")


def run(op, task="Export Q3 as a PDF", script=None):
    async def scenario():
        op.start(task)
        if script:
            await script(op)
        await op.task
        return op.current

    return asyncio.run(scenario())


def test_steps_run_mapped_to_the_real_screen_and_finish(tmp_path):
    mac = FakeMac()
    model = Model(
        [
            {"type": "click", "x": 100, "y": 50, "button": "left"},
            {"type": "type", "text": "Q3 report"},
        ],
        [{"type": "keypress", "keys": ["CMD", "S"]}],
        "Exported Q3 as a PDF to your Desktop.",
    )
    op = operator(tmp_path, mac, model)
    result = run(op)
    assert result.status == "done" and result.result == "Exported Q3 as a PDF to your Desktop."
    assert mac.done == [
        ("click", 200.0, 100.0, "left", 1),
        ("type", "Q3 report"),
        ("keys", ("CMD", "S")),
    ]
    assert result.steps == [
        "Clicking “Export” in Numbers",
        "Typing “Q3 report” in Numbers",
        "Pressing Cmd+S in Numbers",
    ]
    first, second = model.requests[0], model.requests[1]
    assert first["model"] == "gpt-6.1-sol" and first["tools"] == [{"type": "computer"}]
    assert first["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,")
    assert second["previous_response_id"] == "resp-1"
    assert (
        second["input"][0]["type"] == "computer_call_output"
        and second["input"][0]["call_id"] == "call-1"
    )
    assert op.notes == ["Done on your Mac"]
    assert op.recent()[0]["status"] == "done" and op.recent()[0]["actions"] == 3


def test_send_waits_for_go_ahead_and_stop_means_not_clicked(tmp_path):
    send = {"role": "AXButton", "label": "Send", "app": "Mail", "bundle_id": "com.apple.mail"}
    mac = FakeMac(elements={(20.0, 20.0): send})
    model = Model([{"type": "click", "x": 10, "y": 10}], "Sent.")
    op = operator(tmp_path, mac, model)

    async def approve(op):
        asking = await until(op, "approval")
        assert asking.approval["text"] == "Click “Send” in Mail?" and mac.done == []
        op.decide(asking.id, True)

    assert run(op, script=approve).status == "done"
    assert mac.done == [("click", 20.0, 20.0, "left", 1)]

    mac2 = FakeMac(elements={(20.0, 20.0): send})
    op2 = operator(tmp_path, mac2, Model([{"type": "click", "x": 10, "y": 10}], "Sent."))

    async def refuse(op):
        asking = await until(op, "approval")
        op.stop(asking.id)

    result = run(op2, script=refuse)
    assert result.status == "stopped" and mac2.done == []


@pytest.mark.parametrize(
    ("front", "field", "action", "message"),
    [
        (
            ("1Password", "com.1password.1password"),
            None,
            {"type": "click", "x": 1, "y": 1},
            "doesn't operate 1Password",
        ),
        (
            ("Numbers", "x"),
            {"role": "AXSecureTextField", "app": "Safari", "bundle_id": "com.apple.Safari"},
            {"type": "type", "text": "hunter2"},
            "password field",
        ),
    ],
)
def test_never_steps(tmp_path, front, field, action, message):
    mac = FakeMac(front=front, focused=field)
    op = operator(tmp_path, mac, Model([action], "Done."))
    result = run(op)
    assert result.status == "failed" and message in result.result
    assert mac.done == []


def test_bridge_never_clicks_its_own_approval(tmp_path):
    own = {"role": "AXButton", "label": "Go ahead", "app": "Bridge", "bundle_id": BRIDGE_BUNDLE}
    mac = FakeMac(elements={(2.0, 2.0): own})
    result = run(operator(tmp_path, mac, Model([{"type": "click", "x": 1, "y": 1}])))
    assert result.status == "failed" and "Bridge" in result.result and mac.done == []


def test_return_in_a_chat_app_asks_first(tmp_path):
    slack = {
        "role": "AXTextArea",
        "subrole": "",
        "app": "Slack",
        "bundle_id": "com.tinyspeck.slackmacgap",
    }
    mac = FakeMac(focused=slack)
    op = operator(
        tmp_path,
        mac,
        Model(
            [{"type": "type", "text": "on my way"}, {"type": "keypress", "keys": ["ENTER"]}],
            "Sent.",
        ),
    )

    async def approve(op):
        asking = await until(op, "approval")
        assert asking.approval["text"] == "Press Return in Slack? It may send or submit."
        assert mac.done == [("type", "on my way")]
        op.decide(asking.id, True)

    assert run(op, script=approve).status == "done"
    assert mac.done[-1] == ("keys", ("ENTER",))


def test_moving_the_mouse_pauses_and_carry_on_resumes(tmp_path):
    mac = FakeMac()
    op = operator(
        tmp_path,
        mac,
        Model([{"type": "click", "x": 10, "y": 10}, {"type": "click", "x": 20, "y": 20}], "Done."),
    )

    async def takeover(op):
        for _ in range(500):
            if mac.done:
                break
            await asyncio.sleep(0)
        mac.pointer = (900.0, 700.0)  # You grabbed the mouse.
        paused = await until(op, "paused")
        assert paused.approval["kind"] == "pause" and len(mac.done) == 1
        mac.pointer = None
        op.decide(paused.id, True)

    assert run(op, script=takeover).status == "done" and len(mac.done) == 2


def test_every_step_mode_and_the_step_limit(tmp_path):
    mac = FakeMac()
    op = operator(
        tmp_path,
        mac,
        Model([{"type": "scroll", "x": 1, "y": 1, "scroll_x": 0, "scroll_y": 100}], "Done."),
        operator_ask="every",
    )

    async def approve(op):
        asking = await until(op, "approval")
        assert asking.approval["text"] == "Scrolling?"
        op.decide(asking.id, True)

    assert run(op, script=approve).status == "done" and mac.done == [
        ("scroll", 2.0, 2.0, 0.0, 200.0)
    ]
    loop = Model(*([[{"type": "click", "x": 1, "y": 1}]] * (MAX_ACTIONS + 5)))
    result = run(operator(tmp_path, FakeMac(), loop))
    assert (
        result.status == "failed"
        and "50 steps" in result.result
        and result.actions == MAX_ACTIONS + 1
    )


def test_off_unless_turned_on_and_one_task_at_a_time(tmp_path):
    op = operator(tmp_path, FakeMac(), Model("Done."))
    op.settings.operator_enabled = False
    with pytest.raises(OperatorError, match="Use your Mac"):
        asyncio.run(_start(op))
    op.settings.operator_enabled = True
    no_hands = operator(tmp_path, None, Model("Done."))
    with pytest.raises(OperatorError, match="Bridge app on your Mac"):
        asyncio.run(_start(no_hands))

    async def twice():
        op.start("one")
        with pytest.raises(OperatorError, match="already doing"):
            op.start("two")
        await op.task

    asyncio.run(twice())


async def _start(op):
    return op.start("anything")


def test_the_tool_asks_first_and_isnt_on_the_phone(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    tool = agent.executor.registry.get("operate_mac")
    assert tool.risk == RiskLevel.CONFIRM and "operate_mac" in MAC_ONLY_TOOLS


def test_panel_routes_and_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = agent.proactive = None
    agent.operator.hands = None  # Never the real Mac in tests.
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        info = client.get("/api/v1/operator", headers=headers).json()
        assert info["enabled"] is False and info["current"] is None
        saved = client.post(
            "/api/v1/settings/app",
            json={"operator_enabled": True, "operator_ask": "every"},
            headers=headers,
        ).json()
        assert saved["restart_needed"] is False and settings.operator_ask == "every"
        assert (
            client.post(
                "/api/v1/operator/stop", json={"run_id": "abcdef123456"}, headers=headers
            ).status_code
            == 409
        )
        assert client.get("/api/v1/operator/current").status_code == 401
