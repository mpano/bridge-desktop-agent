"""Commitments: promises found in email and Slack, kept honest, nudged and followed up."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.assistant.commitments import CommitmentStore, CommitmentTracker, due_date, grounded
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.phone.access import phone_may_use
from app.tools.productivity.commitments import CommitmentController, render_list

HEADERS = {"Authorization": "Bearer token"}
NOW = datetime(2026, 10, 5, 10, 0).astimezone()  # A Monday morning.


class FakeGmail:
    def __init__(self):
        self.sent_box, self.inbox, self.full, self.sent = [], [], {}, []

    def add(
        self,
        message_id,
        *,
        you_sent,
        frm,
        to,
        body,
        thread="t1",
        date="Mon, 5 Oct 2026 09:00:00 +0200",
    ):
        meta = {"message_id": message_id, "from": frm, "subject": "Contract", "snippet": body[:50]}
        (self.sent_box if you_sent else self.inbox).append(meta)
        self.full[message_id] = {
            **meta,
            "thread_id": thread,
            "to": to,
            "date": date,
            "reply_message_id": f"<{message_id}@mail>",
            "body": body,
        }

    async def search(self, args):
        box = self.sent_box if args.query.startswith("in:sent") else self.inbox
        return {"messages": list(box)}

    async def message(self, args):
        return self.full[args.message_id]

    async def send(self, args):
        self.sent.append(args)
        return {"message_id": "sent-1"}


class FakeLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen = []

    async def complete(self, instructions, data):
        self.seen.append(json.loads(data))
        answer = self.answers.pop(0)
        return answer if isinstance(answer, str) else json.dumps(answer)


def tracker(tmp_path, gmail, llm):
    return CommitmentTracker(CommitmentStore(tmp_path / "db"), llm, gmail=gmail, clock=lambda: NOW)


def test_a_promise_must_really_be_in_the_message():
    text = "Thanks! I’ll send you the signed contract by Friday.\nBest, Akim"
    assert grounded("I'll send you the signed contract by Friday", text)
    assert grounded("I'll send you the signed contract by Friday.", text)
    assert not grounded("I'll send you the invoice tomorrow", text)
    assert not grounded("ok", text)
    today = NOW.date()
    assert due_date("2026-10-09", today) == "2026-10-09"
    assert due_date("next friday", today) is None
    assert due_date("2031-01-01", today) is None


def test_scan_keeps_real_promises_and_drops_invented_ones(tmp_path):
    gmail = FakeGmail()
    gmail.add(
        "m1",
        you_sent=True,
        frm="Me <me@x.test>",
        to="Olivier Martin <olivier@x.test>",
        body="Hi Olivier,\nI'll send you the signed contract by Friday.\nAkim",
    )
    gmail.add(
        "m2",
        you_sent=False,
        frm="Sam Lee <sam@x.test>",
        to="me@x.test",
        body="Let me check with finance and get back to you tomorrow.",
        thread="t2",
    )
    gmail.add(
        "m3",
        you_sent=False,
        frm="Newsletter <no-reply@shop.test>",
        to="me@x.test",
        body="We'll send you deals every week!",
        thread="t3",
    )
    llm = FakeLLM(
        {
            "promises": [
                {
                    "message": "email-m1",
                    "direction": "mine",
                    "person": "Olivier",
                    "what": "Send Olivier the signed contract",
                    "due": "2026-10-09",
                    "quote": "I'll send you the signed contract by Friday",
                },
                {
                    "message": "email-m2",
                    "direction": "theirs",
                    "person": "Sam Lee",
                    "what": "Get back about finance",
                    "due": "2026-10-06",
                    "quote": "Let me check with finance and get back to you tomorrow",
                },
                # Invented: not in the message.
                {
                    "message": "email-m1",
                    "direction": "mine",
                    "person": "Olivier",
                    "what": "Pay the invoice",
                    "due": None,
                    "quote": "I will pay the invoice today",
                },
                # Wrong side: a message you sent can't hold someone else's promise.
                {
                    "message": "email-m1",
                    "direction": "theirs",
                    "person": "Olivier",
                    "what": "Sign it",
                    "due": None,
                    "quote": "I'll send you the signed contract",
                },
            ]
        }
    )
    found = tracker(tmp_path, gmail, llm)
    result = asyncio.run(found.scan())
    assert result["found"] == 2 and result["total"] == 2  # The no-reply newsletter is skipped.
    sent_to_model = llm.seen[0]["messages"]
    assert {m["id"] for m in sent_to_model} == {"email-m1", "email-m2"}
    mine = found.store.items("open")
    assert [(c["direction"], c["person"], c["due"]) for c in mine] == [
        ("theirs", "Sam Lee", "2026-10-06"),
        ("mine", "Olivier", "2026-10-09"),
    ]
    sam = next(c for c in mine if c["direction"] == "theirs")
    assert sam["ref"]["address"] == "sam@x.test" and sam["ref"]["thread_id"] == "t2"
    # Already-read messages aren't read again.
    assert asyncio.run(found.scan())["total"] == 0


def test_a_later_message_that_delivers_closes_the_promise(tmp_path):
    gmail = FakeGmail()
    gmail.add(
        "m1",
        you_sent=True,
        frm="Me",
        to="Olivier <olivier@x.test>",
        body="I'll send you the signed contract by Friday.",
    )
    llm = FakeLLM(
        {
            "promises": [
                {
                    "message": "email-m1",
                    "direction": "mine",
                    "person": "Olivier",
                    "what": "Send Olivier the signed contract",
                    "due": "2026-10-09",
                    "quote": "I'll send you the signed contract by Friday",
                }
            ]
        }
    )
    found = tracker(tmp_path, gmail, llm)
    asyncio.run(found.scan())
    promise = found.store.items("open")[0]
    gmail.add(
        "m4",
        you_sent=True,
        frm="Me",
        to="Olivier <olivier@x.test>",
        body="Here's the signed contract, attached.",
        date="Tue, 6 Oct 2026 09:00:00 +0200",
    )
    llm.answers.append({"promises": [], "fulfilled": [promise["id"], "not-a-real-id"]})
    assert asyncio.run(found.scan())["closed"] == 1
    assert llm.seen[1]["messages"][0]["open_promises"][0]["id"] == promise["id"]
    closed = found.store.get(promise["id"])
    assert closed["status"] == "done" and closed["closed_by"] == "bridge"


def test_nudges_once_a_day_in_working_hours(tmp_path):
    store = CommitmentStore(tmp_path / "db")
    store.add(
        {
            "message": "a",
            "direction": "mine",
            "person": "Olivier",
            "what": "Send the deck",
            "due": "2026-10-05",
            "source": "email",
            "ref": {},
            "quote": "",
        }
    )
    store.add(
        {
            "message": "b",
            "direction": "theirs",
            "person": "Sam",
            "what": "Send numbers",
            "due": "2026-10-02",
            "source": "email",
            "ref": {},
            "quote": "",
        }
    )
    store.add(
        {
            "message": "c",
            "direction": "theirs",
            "person": "Kim",
            "what": "Reply",
            "due": "2026-10-05",
            "source": "email",
            "ref": {},
            "quote": "",
        }
    )
    notes = []
    clock = {"now": NOW}
    found = CommitmentTracker(store, FakeLLM(), clock=lambda: clock["now"])

    async def notify(title, body, view):
        notes.append((title, body, view))

    found.notify = notify
    assert asyncio.run(found.nudge()) == 2  # Kim's is due today: not late yet.
    assert notes[0][0] == "You promised Olivier: due today"
    assert notes[1][0] == "Waiting on Sam" and "Follow up?" in notes[1][1]
    assert asyncio.run(found.nudge()) == 0  # Once a day.
    clock["now"] = NOW.replace(hour=22)
    store.update(store.items("open")[0]["id"], nudged=None)
    assert asyncio.run(found.nudge()) == 0  # Not at night.


def make(tmp_path, gmail, llm):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = None
    agent.proactive = None
    agent.commitments = tracker(tmp_path, gmail, llm)
    return create_app(settings, agent, enable_ui=True), agent


def test_today_lists_updates_and_follows_up_in_the_same_thread(tmp_path):
    gmail = FakeGmail()
    gmail.add(
        "m2",
        you_sent=False,
        frm="Sam Lee <sam@x.test>",
        to="me@x.test",
        body="Let me check with finance and get back to you tomorrow.",
        thread="t2",
    )
    llm = FakeLLM(
        {
            "promises": [
                {
                    "message": "email-m2",
                    "direction": "theirs",
                    "person": "Sam Lee",
                    "what": "Get back about finance",
                    "due": "2026-10-02",
                    "quote": "Let me check with finance and get back to you tomorrow",
                }
            ]
        },
        "Hi Sam, any news from finance?",
    )
    app, agent = make(tmp_path, gmail, llm)
    asyncio.run(agent.commitments.scan())
    with TestClient(app) as client:
        listed = client.get("/api/v1/commitments", headers=HEADERS).json()
        assert listed["connected"] and listed["mine"] == []
        sam = listed["theirs"][0]
        assert sam["can_follow_up"] and sam["link"].endswith("#all/t2")
        assert "ref" not in sam  # Where it's sent comes from the stored message only.

        draft = client.post("/api/v1/commitments/draft", json={"id": sam["id"]}, headers=HEADERS)
        assert draft.json()["body"] == "Hi Sam, any news from finance?"
        assert llm.seen[-1]["they_promised"] == "Get back about finance"
        sent = client.post(
            "/api/v1/commitments/send",
            json={"id": sam["id"], "body": "Hi Sam, any news?", "to": "evil@x.test"},
            headers=HEADERS,
        )
        assert sent.status_code == 422  # Recipients can't come from the page.
        sent = client.post(
            "/api/v1/commitments/send",
            json={"id": sam["id"], "body": "Hi Sam, any news?"},
            headers=HEADERS,
        ).json()
        assert sent == {"sent": True, "to": "sam@x.test"}
        mail = gmail.sent[0]
        assert mail.to == ["sam@x.test"] and mail.thread_id == "t2"
        assert mail.in_reply_to == "<m2@mail>" and mail.subject == "Re: Contract"

        moved = client.post(
            "/api/v1/commitments/update",
            json={"id": sam["id"], "due": "2026-10-12"},
            headers=HEADERS,
        ).json()
        assert moved["due"] == "2026-10-12" and moved["followed_up"]
        done = client.post(
            "/api/v1/commitments/update",
            json={"id": sam["id"], "status": "done"},
            headers=HEADERS,
        ).json()
        assert done["status"] == "done" and done["closed_by"] == "you"
        assert client.get("/api/v1/commitments", headers=HEADERS).json()["theirs"] == []
        bad = client.post("/api/v1/commitments/update", json={"id": "x"}, headers=HEADERS)
        assert bad.status_code == 422
        assert client.get("/api/v1/commitments").status_code == 401


def test_ask_can_list_add_and_close_promises(tmp_path):
    found = tracker(tmp_path, None, FakeLLM())
    tools = CommitmentController(found)
    added = asyncio.run(
        tools.add(
            SimpleNamespace(
                direction="mine",
                person="Sam",
                what="Send the budget",
                due=NOW.date(),
                model_dump=lambda mode: {
                    "direction": "mine",
                    "person": "Sam",
                    "what": "Send the budget",
                    "due": NOW.date().isoformat(),
                },
            )
        )
    )
    assert added["added"]
    listed = asyncio.run(tools.list(SimpleNamespace(direction="all", person="sam")))
    assert "You owe:\n• Send the budget (Sam)" in render_list(listed)
    done = asyncio.run(tools.done(SimpleNamespace(what="budget for Sam")))
    assert done == {"done": True, "what": "Send the budget", "person": "Sam"}
    assert found.store.items("open") == []


def test_the_phone_can_see_and_answer_promises():
    for path in ("/api/v1/commitments/update", "/api/v1/commitments/send"):
        assert phone_may_use("POST", path)
    assert phone_may_use("GET", "/api/v1/commitments")
