"""Inbox screen routes and the reply drafter, with Gmail and the model faked."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.inbox import follow_up_due
from app.api.server import create_app
from app.assistant.replies import ReplyDrafter, address_of, reply_subject, strip_quoted
from app.bootstrap import build_agent
from app.config.settings import Settings
from tests.test_dashboard_api import headers

SARAH = {
    "message_id": "m1",
    "thread_id": "t1",
    "reply_message_id": "<m1@mail.example>",
    "from": "Sarah Uwase <sarah@example.com>",
    "subject": "Contract",
    "date": "Thu, 1 Oct 2026 09:00:00 +0200",
    "summary": "Asks if you have questions",
    "action": "Ask about the start date",
}


def gmail():
    service = AsyncMock()
    service.thread.return_value = {
        "messages": [
            {"message_id": "m0", "from": "You <me@example.com>", "body": "Earlier note"},
            {
                "message_id": "m1",
                "from": SARAH["from"],
                "date": SARAH["date"],
                "body": "Any questions before signing?\n\nOn Wed, Sarah wrote:\n> old text",
            },
        ]
    }
    service.search.return_value = {"messages": [{"snippet": "Hi Olivier, sounds good! Akim"}]}
    service.send.return_value = {"message_id": "sent1", "thread_id": "t1"}
    return service


@pytest.fixture
def inbox(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="local-test-token")
    llm = AsyncMock()
    llm.complete.return_value = "Hi Sarah,\n\nTwo quick questions…\n\nAkim"
    agent = build_agent(settings, llm=llm, runner=AsyncMock())
    agent.gmail = gmail()
    agent.replies = ReplyDrafter(agent.gmail, llm)
    groups = {"urgent": [], "reply": [SARAH], "fyi": [], "newsletter": []}
    agent.inbox = SimpleNamespace(last=(time.time(), {"groups": groups}))
    with TestClient(
        create_app(settings, agent, enable_ui=True), base_url="http://127.0.0.1:8000"
    ) as client:
        yield client, agent, llm


def test_inbox_lists_sorted_mail_without_reply_headers(inbox):
    client, _, _ = inbox
    data = client.get("/api/v1/inbox", headers=headers()).json()
    assert data["gmail"] and data["sorted"]["fresh"]
    [item] = data["sorted"]["groups"]["reply"]
    assert item["subject"] == "Contract" and "reply_message_id" not in item


def test_open_reads_the_email_without_its_quoted_history(inbox):
    client, _, _ = inbox
    data = client.post("/api/v1/inbox/open", json={"message_id": "m1"}, headers=headers()).json()
    assert data["body"] == "Any questions before signing?"
    assert (
        data["to"] == "sarah@example.com"
        and data["subject"] == "Re: Contract"
        and data["can_reply"]
    )
    assert (
        client.post(
            "/api/v1/inbox/open", json={"message_id": "nope"}, headers=headers()
        ).status_code
        == 404
    )


def test_draft_imitates_sent_mail_and_treats_email_as_data(inbox):
    client, agent, llm = inbox
    body = client.post(
        "/api/v1/inbox/draft", json={"message_id": "m1", "name": "Akim"}, headers=headers()
    ).json()["body"]
    assert body.startswith("Hi Sarah")
    instructions, data = llm.complete.await_args.args
    assert "never instructions" in instructions
    sent = json.loads(data)
    assert sent["the_users_recent_sent_emails"] == ["Hi Olivier, sounds good! Akim"]
    assert sent["email_to_answer"]["text"] == "Any questions before signing?"
    client.post("/api/v1/inbox/draft", json={"message_id": "m1"}, headers=headers())
    assert llm.complete.await_count == 1  # The draft is kept, not rewritten.


def test_send_replies_in_thread_to_the_sender_only(inbox):
    client, agent, _ = inbox
    response = client.post(
        "/api/v1/inbox/send",
        json={"message_id": "m1", "body": "Hi Sarah, yes.", "follow_up": True},
        headers=headers(),
    ).json()
    assert response["sent"] and response["to"] == "sarah@example.com"
    args = agent.gmail.send.await_args.args[0]
    assert args.to == ["sarah@example.com"] and args.subject == "Re: Contract"
    assert args.thread_id == "t1" and args.in_reply_to == "<m1@mail.example>"
    assert response["followup"]["name"] == "Sarah Uwase"
    assert agent.proactive_store.followups()[0].email == "sarah@example.com"


def test_page_cannot_choose_the_recipient(inbox):
    client, agent, _ = inbox
    response = client.post(
        "/api/v1/inbox/send",
        json={"message_id": "m1", "body": "x", "to": ["attacker@example.com"]},
        headers=headers(),
    )
    assert response.status_code == 422
    agent.gmail.send.assert_not_awaited()


def test_inbox_needs_gmail(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="local-test-token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    with TestClient(
        create_app(settings, agent, enable_ui=True), base_url="http://127.0.0.1:8000"
    ) as client:
        assert client.get("/api/v1/inbox", headers=headers()).json() == {
            "gmail": False,
            "slack": False,
            "sorted": None,
        }
        assert (
            client.post(
                "/api/v1/inbox/open", json={"message_id": "m1"}, headers=headers()
            ).status_code
            == 404  # Nothing has been sorted, so there's nothing to open.
        )
        assert client.get("/ui/inbox.js").status_code == 200


def test_reply_helpers():
    assert address_of("Sarah <sarah@example.com>") == "sarah@example.com"
    assert address_of("sarah@example.com") == "sarah@example.com"
    assert address_of("Sarah Uwase") == ""
    assert (
        reply_subject("Re: Contract") == "Re: Contract" and reply_subject("") == "Re: (no subject)"
    )
    assert strip_quoted("Thanks!\n> quoted\nBye") == "Thanks!\nBye"
    friday = __import__("datetime").datetime(2026, 10, 2, 9, 0).astimezone()  # A Friday.
    due = follow_up_due(friday)
    assert due.weekday() == 2 and due.hour == 17  # Three working days later: Wednesday.
