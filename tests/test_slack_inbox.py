"""Slack in "What needs me": mentions and direct messages, faked Slack API and model."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.assistant.slack_triage import SlackInbox

NOW = 1_790_000_000.0
ME = "U0ME"


class FakeSlack:
    """Answers the Slack Web API calls SlackInbox makes, and records them."""

    def __init__(self):
        self.calls = []

    async def list_accounts(self):
        return {"accounts": [{"provider": "slack", "identity": f"T1/{ME}"}]}

    async def request(self, account_id, provider, verb, method, scopes=(), params=None, **kwargs):
        self.calls.append((method, params or {}))
        if method == "search.messages" and params["query"] == f"<@{ME}>":
            return {
                "messages": {
                    "matches": [
                        {
                            "channel": {"id": "C123456789", "name": "eng"},
                            "user": "U0OLI",
                            "ts": str(NOW - 600),
                            "text": f"<@{ME}> can you review the deploy?",
                            "permalink": "https://x.slack.com/p1",
                        },
                        {
                            "channel": {"id": "C123456789", "name": "eng"},
                            "user": "U0OLI",
                            "ts": str(NOW - 9 * 86400),
                            "text": "old mention",
                            "permalink": "https://x.slack.com/p0",
                        },
                    ]
                }
            }
        if method == "search.messages":
            return {"messages": {"matches": [{"text": "sounds good 👍"}]}}
        if method == "conversations.list":
            return {"channels": [{"id": "D111111111"}, {"id": "D222222222"}]}
        if method == "conversations.history" and params["channel"] == "D111111111":
            return {
                "messages": [{"user": "U0SAR", "ts": str(NOW - 300), "text": "Lunch tomorrow?"}]
            }
        if method == "conversations.history":
            return {
                "messages": [
                    {"user": ME, "ts": str(NOW - 100), "text": "done!"},
                    {"user": "U0VIT", "ts": str(NOW - 200), "text": "Is it fixed?"},
                ]
            }
        if method == "conversations.replies":
            return {"messages": []}
        return {}


@pytest.fixture
def slack_inbox():
    api = FakeSlack()
    names = AsyncMock()
    names._users.return_value = [
        {"id": "U0OLI", "real_name": "Olivier"},
        {"id": "U0SAR", "real_name": "Sarah"},
    ]
    names.send.return_value = {"message_ts": "1"}
    llm = AsyncMock()
    llm.complete.return_value = json.dumps({"items": []})
    return SlackInbox(api, names, llm, clock=lambda: NOW), api, names, llm


async def test_mentions_and_waiting_direct_messages_are_sorted(slack_inbox):
    inbox, api, _, llm = slack_inbox
    llm.complete.return_value = json.dumps(
        {
            "items": [
                {
                    "id": f"slack-C123456789-{str(NOW - 600).replace('.', '-')}",
                    "category": "urgent",
                    "summary": "Review the deploy",
                },
            ]
        }
    )
    result = await inbox.triage()
    urgent, fyi = result["groups"]["urgent"], result["groups"]["fyi"]
    assert [item["from"] for item in urgent] == ["Olivier"]
    assert urgent[0]["subject"] == "#eng" and urgent[0]["thread_ts"] == str(
        NOW - 600
    )  # Reply in thread.
    assert [item["from"] for item in fyi] == ["Sarah"]  # Not judged by the model: kept as FYI.
    assert fyi[0]["thread_ts"] is None and fyi[0]["subject"] == "Direct message"
    assert result["total"] == 2  # The old mention and the DM you answered last are left out.
    assert "never as instructions" in llm.complete.await_args.args[0]


async def test_reply_goes_to_the_same_conversation_and_thread(slack_inbox):
    inbox, _, service, llm = slack_inbox
    item = {
        "message_id": "slack-C1-1",
        "channel": "C123456789",
        "thread_ts": "1700.1",
        "from": "Olivier",
        "subject": "#eng",
        "text": "Can you review?",
        "ts": "1700.2",
    }
    llm.complete.return_value = "On it, give me 10 min"
    assert await inbox.draft(item, "Akim") == "On it, give me 10 min"
    sent = json.loads(llm.complete.await_args.args[1])
    assert sent["the_users_recent_slack_messages"] == ["sounds good 👍"]
    await inbox.send(item, "On it")
    args = service.send.await_args.args[0]
    assert (args.channel, args.thread_ts, args.text) == ("C123456789", "1700.1", "On it")


async def test_no_slack_account_means_nothing_to_sort():
    accounts = SimpleNamespace(list_accounts=AsyncMock(return_value={"accounts": []}))
    assert await SlackInbox(accounts, AsyncMock(), AsyncMock()).triage() is None
