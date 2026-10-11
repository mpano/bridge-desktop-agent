"""Bridge learns from your corrections: noticing, proposing, testing, using lessons."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.assistant.commitments import CommitmentStore, CommitmentTracker
from app.assistant.learning import CLASSIFY, JUDGE, PROPOSE, REWRITE, Learning
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm import prompts

FRIDAY = datetime(2026, 10, 9, 17, 0).timestamp()
RULE = "Thanks or offers like 'happy to help anytime' are not promises."


class Model:
    """Answers according to which instructions Bridge sent."""

    def __init__(self, propose=None, helps=True):
        self.propose = propose or [{"rule": RULE, "why": "You dismissed these.", "cases": []}]
        self.helps = helps
        self.calls = []

    async def complete(self, instructions, text):
        data = json.loads(text) if text.startswith("{") else {}
        self.calls.append(instructions.split("\n")[0][:40])
        if instructions.startswith(PROPOSE[:40]):
            return json.dumps({"lessons": self.propose})
        if instructions.startswith(CLASSIFY[:40]):
            with_rule = RULE in data.get("rules", [])
            return json.dumps(
                {
                    "verdicts": [
                        {
                            "id": q["id"],
                            "promise": q["id"].startswith("r") or not (with_rule and self.helps),
                        }
                        for q in data["quotes"]
                    ]
                }
            )
        if instructions.startswith(REWRITE[:40]):
            return (
                "Sure, Thursday works."
                if self.helps
                else data["draft"] + " Let me know what you think!!"
            )
        if instructions.startswith(JUDGE[:40]):
            return json.dumps({"helped": [c["id"] for c in data["cases"]], "too_broad": False})
        return "{}"


def learning(tmp_path, model, **settings):
    config = SimpleNamespace(learning_enabled=True, **settings)
    return Learning(tmp_path / "db", model, config, clock=lambda: FRIDAY)


def test_drafts_you_rewrote_are_noticed(tmp_path):
    learn = learning(tmp_path, Model())
    learn.draft_sent(
        "email",
        "Sam",
        "Hi Sam, Thursday works for me. Best, Akim",
        "Hi Sam, Thursday works for me. Best, Akim",
    )
    assert learn.cases() == []  # Sent as drafted: nothing to learn.
    learn.draft_sent(
        "email",
        "Sam",
        "Dear Sam, I hope this finds you well. I would be delighted to meet on Thursday.",
        "Sure, Thursday works.",
    )
    [case] = learn.cases()
    assert case["area"] == "drafts" and case["detail"]["sent"] == "Sure, Thursday works."


def test_promise_lessons_are_tested_on_past_cases_before_you_see_them(tmp_path):
    store = CommitmentStore(tmp_path / "db")
    store.add(
        {
            "message": "m",
            "direction": "mine",
            "person": "Olivier",
            "what": "Send the deck",
            "due": None,
            "source": "email",
            "ref": {},
            "quote": "I'll send the deck by Friday",
        }
    )
    model = Model()
    learn = learning(tmp_path, model)
    learn.commitments = CommitmentTracker(store, model)
    for quote in ("Happy to help anytime!", "Always happy to help, just ask"):
        learn.record("promises", "not_a_promise", quote, {"quote": quote})
    assert asyncio.run(learn.propose()) == 1
    [rule] = learn.rules("proposed")
    assert rule["rule"] == RULE and rule["tested"]["method"] == "replayed"
    assert (
        rule["tested"]["after"] > rule["tested"]["before"]
    )  # Dismissed ones now right; the real one kept.
    assert learn.cases(unused=True) == []
    assert learn.lessons("promises") == ""  # Not in use until you accept it.
    learn.decide(rule["id"], "active")
    assert RULE in learn.lessons("promises") and learn.lessons("drafts") == ""


def test_a_lesson_that_makes_things_worse_is_never_shown(tmp_path):
    learn = learning(
        tmp_path,
        Model(
            helps=False,
            propose=[{"rule": "Write longer, warmer replies.", "why": "x", "cases": []}],
        ),
    )
    for _ in range(2):
        learn.draft_sent(
            "email",
            "Sam",
            "Dear Sam, I hope this finds you well. Thursday is great for me.",
            "Sure, Thursday works.",
        )
    assert asyncio.run(learn.propose()) == 0 and learn.rules() == []


def test_accepted_lessons_reach_ask(tmp_path):
    learn = learning(tmp_path, Model())
    with learn._db() as db:
        db.execute(
            "INSERT INTO learn_rules (area, rule, why, status, created) "
            "VALUES ('ask', 'Answer in one sentence.', '', 'active', 0)"
        )
    learn._active = None
    prompts.lessons_provider = learn.lessons
    try:
        assert "- Answer in one sentence." in prompts.system_prompt()
        learn.settings.learning_enabled = False
        assert "Answer in one sentence" not in prompts.system_prompt()  # Off means off.
    finally:
        prompts.lessons_provider = None


def test_friday_report_once_a_week(tmp_path):
    notes = []

    async def notify(title, body, view):
        notes.append((title, body))

    learn = learning(tmp_path, Model())
    learn.notify = notify
    learn.record("ask", "failed", "Book a table", {"request": "Book a table"})
    learn.record("ask", "thumbs_down", "Too long", {"request": "Brief me"})
    asyncio.run(learn.check())
    asyncio.run(learn.check())
    assert len(notes) == 1 and notes[0][0] == "What Bridge learned this week"
    week = learn.week()
    assert next(line for line in week["lines"] if line["area"] == "ask")["this_week"] == 2


def test_corrections_are_noticed_across_bridge(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = agent.proactive = None
    headers = {"Authorization": "Bearer token"}
    store = agent.commitments.store
    store.add(
        {
            "message": "m",
            "direction": "mine",
            "person": "Kim",
            "what": "Help anytime",
            "due": None,
            "source": "email",
            "ref": {},
            "quote": "happy to help anytime",
        }
    )
    promise = store.items("open")[0]
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        client.post(
            "/api/v1/commitments/update",
            json={"id": promise["id"], "status": "dismissed"},
            headers=headers,
        )
        agent.transcript = [
            {"role": "user", "text": "Brief me", "request_id": "abcdef12"},
            {"role": "assistant", "text": "A very long brief…", "request_id": "abcdef12"},
        ]
        client.post(
            "/api/v1/learning/feedback",
            json={"request_id": "abcdef12", "good": False, "note": "Too long"},
            headers=headers,
        )
        client.post(
            "/api/v1/learning/feedback",
            json={"request_id": "abcdef12", "good": True},
            headers=headers,
        )
        data = client.get("/api/v1/learning", headers=headers).json()
        assert data["waiting"] == 2 and data["enabled"]
        kinds = {(c["area"], c["kind"]) for c in agent.learning.cases()}
        assert kinds == {("promises", "not_a_promise"), ("ask", "thumbs_down")}
        client.post("/api/v1/settings/app", json={"learning_enabled": False}, headers=headers)
        client.post(
            "/api/v1/learning/feedback",
            json={"request_id": "abcdef12", "good": False},
            headers=headers,
        )
        assert len(agent.learning.cases()) == 2  # Off: nothing new is noted.
        client.post("/api/v1/learning/forget", headers=headers)
        assert agent.learning.cases() == []
