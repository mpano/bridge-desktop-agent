"""Richer email, Slack, calendar and Spotify actions. Providers and macOS are mocked."""

import base64
import json
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.agent.executor import Executor
from app.bootstrap import build_agent
from app.integrations.accounts import AccountManager
from app.integrations.models import IntegrationError
from app.integrations.transport import ProviderHTTP
from app.llm.models import LLMResponse, ToolCall
from app.llm.prompts import system_prompt
from app.tools.email.compose import ComposeInput, EmailComposer, InboxSearchInput
from app.tools.integrations.register import register
from app.tools.registry import ToolRegistry
from app.tools.spotify.spotify import SpotifyController, SpotifyInput, SpotifyVolumeInput
from tests.test_connected_services import MemoryCredentials, account, manager, settings


def tools(service, **kwargs):
    registry = ToolRegistry()
    register(registry, service, **kwargs)
    return registry, Executor(registry)


async def run(executor, name, approved=False, **arguments):
    return await executor.execute(
        ToolCall(call_id="c", name=name, arguments=arguments), "request", approved=approved
    )


# Accounts


async def test_account_is_selected_automatically_by_identity_or_as_the_only_one():
    def handler(req):
        return httpx.Response(200, json={"messages": []})

    service, _, _ = manager(handler)
    _, executor = tools(service)
    assert (await run(executor, "email_search", query="is:unread"))["success"]
    result = await run(executor, "email_search", query="x", account_id="user@example.com")
    assert result["success"]


async def test_account_selection_explains_missing_or_ambiguous_accounts():
    store = MemoryCredentials()
    service = AccountManager(settings(), store, ProviderHTTP(httpx.MockTransport(lambda r: None)))
    with pytest.raises(IntegrationError, match="No Gmail account is connected"):
        await service.request(None, "gmail", "GET", "messages")
    first, second = account(), account()
    second.account_id, second.identity = "gmail:other@example.com", "other@example.com"
    store.values = {first.account_id: first, second.account_id: second}
    with pytest.raises(IntegrationError, match="other@example.com, user@example.com"):
        await service.request(None, "gmail", "GET", "messages")


# Local display with privacy


async def test_withheld_results_are_shown_locally_but_never_sent_to_the_model(tmp_path):
    def handler(req):
        if req.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(
            200,
            json={
                "id": "m1",
                "snippet": "Quarterly numbers attached",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Alex <alex@example.com>"},
                        {"name": "Subject", "value": "Q3 report"},
                    ]
                },
            },
        )

    service, _, _ = manager(handler)
    llm = AsyncMock()
    llm.generate_response.side_effect = [
        LLMResponse(calls=[ToolCall(call_id="1", name="email_search", arguments={"query": "q"})]),
        LLMResponse(text="Here are your emails:"),
    ]
    agent = build_agent(
        settings(database_path=tmp_path / "db"), llm=llm, runner=AsyncMock(), accounts=service
    )
    try:
        result = await agent.message("Any email from Alex?")
        assert result["message"].startswith("Here are your emails:")
        assert "Q3 report" in result["message"] and "Alex" in result["message"]
        sent_to_model = json.dumps(llm.generate_response.call_args_list[-1].args[0])
        assert "Q3 report" not in sent_to_model
        assert "Q3 report" not in json.dumps(agent.get_workflow(result["request_id"]))
    finally:
        await agent.close()


# Slack


def slack_handler(calls, users=None, channels=None):
    users = users or [
        {"id": "U1", "name": "alex", "real_name": "Alex Kim", "profile": {}},
        {"id": "U2", "name": "sam", "real_name": "Sam Lee", "profile": {"display_name": "Sammy"}},
    ]
    channels = channels or [{"id": "C11111111", "name": "general"}]

    def handler(req):
        calls.append(req)
        method = req.url.path.rsplit("/", 1)[-1]
        if method == "conversations.list":
            return httpx.Response(200, json={"ok": True, "channels": channels})
        if method == "users.list":
            return httpx.Response(200, json={"ok": True, "members": users})
        if method == "conversations.open":
            return httpx.Response(200, json={"ok": True, "channel": {"id": "D22222222"}})
        if method == "conversations.history":
            messages = [
                {"user": "U1", "text": "second", "ts": "2.0"},
                {"user": "U2", "text": "first", "ts": "1.0"},
            ]
            return httpx.Response(200, json={"ok": True, "messages": messages})
        body = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "channel": body["channel"], "ts": "1.1"})

    return handler


@pytest.mark.parametrize(
    "destination,channel,label",
    [
        ("#general", "C11111111", "#general"),
        ("general", "C11111111", "#general"),
        ("@alex", "D22222222", "@Alex Kim"),
        ("Sammy", "D22222222", "@Sam Lee"),
    ],
)
async def test_slack_sends_to_names_after_approval(destination, channel, label):
    calls = []
    service, _, _ = manager(slack_handler(calls), "slack")
    _, executor = tools(service)
    pending = await run(executor, "slack_send_message", channel=destination, text="Hi")
    assert pending["status"] == "confirmation_required" and not calls
    result = await run(executor, "slack_send_message", True, channel=destination, text="Hi")
    assert result["success"], result
    assert json.loads(calls[-1].content)["channel"] == channel
    assert label in result["display"]


async def test_slack_refuses_ambiguous_or_unknown_people():
    users = [
        {"id": "U1", "name": "alex", "real_name": "Alex Kim", "profile": {}},
        {"id": "U3", "name": "alex2", "real_name": "alex", "profile": {}},
    ]
    calls = []
    service, _, _ = manager(slack_handler(calls, users=users), "slack")
    _, executor = tools(service)
    result = await run(executor, "slack_send_message", True, channel="@alex", text="Hi")
    assert not result["success"] and "Several Slack people" in result["error"]
    result = await run(executor, "slack_send_message", True, channel="#nowhere", text="Hi")
    assert not result["success"] and "No Slack channel or person" in result["error"]
    assert not any(req.url.path.endswith("chat.postMessage") for req in calls)


async def test_slack_reads_channel_history_oldest_first_with_names():
    service, _, _ = manager(slack_handler([]), "slack")
    _, executor = tools(service)
    result = await run(executor, "slack_read_channel", channel="#general")
    assert result["success"]
    assert result["display"].index("Sam Lee: first") < result["display"].index("Alex Kim: second")


# Calendar

ZONE = timezone(timedelta(hours=2))


def at(hour, minute=0):
    return datetime(2026, 10, 1, hour, minute, tzinfo=ZONE).isoformat()


def event(event_id, title, start, end):
    return {
        "id": event_id,
        "summary": title,
        "start": {"dateTime": at(start)},
        "end": {"dateTime": at(end)},
    }


def calendar_handler(calls, items):
    def handler(req):
        calls.append(req)
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"items": items})

    return handler


async def test_calendar_free_time_skips_busy_events():
    items = [
        event("a", "Standup", 9, 10),
        event("b", "Lunch", 12, 13),
        {
            "id": "c",
            "summary": "Focus",
            "transparency": "transparent",
            "start": {"dateTime": at(14)},
            "end": {"dateTime": at(15)},
        },
    ]
    service, _, _ = manager(calendar_handler([], items), "google_calendar")
    _, executor = tools(service)
    result = await run(executor, "calendar_free_time", start=at(9), end=at(17), min_minutes=60)
    assert result["success"]
    assert result["result"]["free"] == [
        {"start": at(10), "end": at(12)},
        {"start": at(13), "end": at(17)},
    ]
    assert "Free time" in result["display"]


async def test_calendar_delete_needs_exactly_one_title_match_and_approval():
    items = [
        event("a1", "Standup", 9, 10),
        event("b2", "Review", 11, 12),
        event("b3", "Review", 15, 16),
    ]
    calls = []
    service, _, _ = manager(calendar_handler(calls, items), "google_calendar")
    _, executor = tools(service)
    window = {"start": at(8), "end": at(18)}
    pending = await run(executor, "calendar_delete_event", title="standup", **window)
    assert pending["status"] == "confirmation_required" and not calls
    result = await run(executor, "calendar_delete_event", True, title="standup", **window)
    assert result["success"] and calls[-1].method == "DELETE"
    assert calls[-1].url.path.endswith("/events/a1")
    result = await run(executor, "calendar_delete_event", True, title="Review", **window)
    assert not result["success"] and "2 events" in result["error"]


# Gmail


async def test_gmail_draft_saves_without_sending():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"id": "draft-1"})

    service, _, _ = manager(handler)
    _, executor = tools(service)
    arguments = {"to": ["alex@example.com"], "subject": "Hi", "body": "Draft body"}
    result = await run(executor, "email_create_draft", True, **arguments)
    assert result["success"]
    assert calls[-1].url.path.endswith("/drafts")
    raw = json.loads(calls[-1].content)["message"]["raw"]
    assert "Draft body" in message_from_bytes(base64.urlsafe_b64decode(raw)).get_payload()


# Spotify


async def test_spotify_play_search_plays_top_result_in_desktop_app():
    uri = "spotify:track:" + "b" * 22

    def handler(req):
        item = {"name": "Blinding Lights", "uri": uri, "artists": [{"name": "The Weeknd"}]}
        return httpx.Response(200, json={"tracks": {"items": [item]}})

    service, _, _ = manager(handler, "spotify")
    desktop = AsyncMock()
    _, executor = tools(service, desktop_spotify=desktop)
    result = await run(executor, "spotify_play_search", query="blinding lights")
    assert result["success"]
    desktop.assert_awaited_once_with(uri)
    assert "Blinding Lights — The Weeknd" in result["display"]


async def test_spotify_desktop_values_are_arguments_not_script_source():
    runner, script = AsyncMock(), AsyncMock()
    controller = SpotifyController(runner, script)
    await controller.set_volume(SpotifyVolumeInput(level=40))
    argv = runner.run.await_args.args
    assert argv[0] == "/usr/bin/osascript" and argv[-1] == "40" and "40" not in argv[2]
    await controller.play_uri('spotify:track:x" & do shell script "bad')
    assert "do shell script" not in runner.run.await_args.args[2]


async def test_spotify_now_playing_is_parsed_and_rendered():
    script = AsyncMock()
    script.run.return_value = "playing\nSong\nArtist\nAlbum\nspotify:track:x\n70"
    controller = SpotifyController(AsyncMock(), script)
    data = await controller.control(SpotifyInput(action="now_playing"))
    assert data["track"] == "Song" and data["state"] == "playing"


# Email without an account


async def test_compose_in_browser_prefills_gmail_and_never_sends():
    runner, open_url = AsyncMock(), AsyncMock()
    composer = EmailComposer(runner, open_url)
    await composer.compose(
        ComposeInput(to=["alex@example.com"], subject="Lunch?", body="Tomorrow at 12 & more")
    )
    url = urlparse(open_url.await_args.args[0])
    query = parse_qs(url.query)
    assert url.netloc == "mail.google.com"
    assert query["to"] == ["alex@example.com"] and query["body"] == ["Tomorrow at 12 & more"]
    await composer.compose(ComposeInput(to=["alex@example.com"], subject="Hi", client="mail_app"))
    assert runner.run.await_args.args[1].startswith("mailto:alex@example.com?")
    await composer.search(InboxSearchInput(query="from:alex is:unread"))
    assert open_url.await_args.args[0].endswith("#search/from%3Aalex%20is%3Aunread")


def test_system_prompt_includes_local_date_for_relative_requests():
    moment = datetime(2026, 10, 1, 9, 30, tzinfo=ZONE)
    text = system_prompt(moment)
    assert "Thursday 2026-10-01T09:30:00+02:00" in text and "+02:00" in text
