"""Connection activity log: what happened, never content or tokens."""

import json

import httpx
import pytest

from app.integrations.activity import SQLiteActivity, describe
from app.integrations.models import IntegrationError
from tests.test_connected_services import manager


def test_actions_are_described_without_ids_or_content():
    assert describe("gmail", "POST", "messages/send") == "Sent an email"
    assert describe("gmail", "GET", "messages/abc123") == "Searched or read email"
    assert (
        describe("google_calendar", "DELETE", "calendars/primary/events/x1") == "Deleted an event"
    )
    assert describe("slack", "POST", "chat.postMessage") == "Sent a message"
    assert describe("spotify", "PUT", "me/player/play") == "Started playback"
    assert describe("spotify", "GET", "unknown/thing") == "API request"


async def test_requests_sign_ins_and_disconnects_are_logged_without_secrets():
    def handler(req):
        if req.url.path.endswith("/send"):
            return httpx.Response(403)
        return httpx.Response(200, json={"messages": []})

    service, _, value = manager(handler)
    await service.request(None, "gmail", "GET", "messages", params={"q": "secret search"})
    with pytest.raises(IntegrationError):
        await service.request(None, "gmail", "POST", "messages/send", json={"raw": "private"})
    await service.begin("slack", True)
    with pytest.raises(IntegrationError):
        await service.finish("slack", "wrong-state", "code")
    await service.disconnect(value.account_id)

    log = service.activity.recent()
    events = [(item["provider"], item["event"], item["ok"]) for item in log]
    assert events == [
        ("gmail", "Disconnected", True),
        ("slack", "Sign-in failed", False),
        ("slack", "Sign-in started", True),
        ("gmail", "Sent an email", False),
        ("gmail", "Searched or read email", True),
    ]
    assert log[3]["account"] == "user@example.com" and "denied" in log[3]["detail"]
    dumped = json.dumps(log)
    assert "secret search" not in dumped and "private" not in dumped
    assert "access-secret" not in dumped and "refresh-secret" not in dumped


async def test_catalog_includes_activity_and_connection_time():
    service, store, value = manager(lambda req: httpx.Response(200, json={}))
    store.values[value.account_id].connected_at = 1234.0
    await service.request(None, "gmail", "GET", "profile")
    catalog = await service.catalog()
    assert catalog["accounts"][0]["connected_at"] == 1234.0
    assert catalog["activity"][0]["event"] == "Checked profile"


def test_sqlite_activity_survives_restart_and_stays_bounded(tmp_path):
    first = SQLiteActivity(tmp_path / "db")
    for index in range(510):
        first.record("spotify", f"event {index}")
    second = SQLiteActivity(tmp_path / "db", limit=5)
    assert [item["event"] for item in second.recent()] == [
        f"event {i}" for i in range(509, 504, -1)
    ]
    import sqlite3

    with sqlite3.connect(tmp_path / "db") as db:
        (count,) = db.execute("SELECT COUNT(*) FROM connection_activity").fetchone()
    assert count <= 501
