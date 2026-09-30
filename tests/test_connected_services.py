"""Provider calls are mocked: no accounts, Keychain entries or real messages touched."""

import base64
import hashlib
import json
import time
from email import message_from_bytes
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agent.executor import Executor
from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.integrations.accounts import AccountManager
from app.integrations.credentials import KeychainCredentials
from app.integrations.email import message_view
from app.integrations.models import Account, IntegrationError
from app.integrations.providers import GOOGLE, PROVIDERS
from app.integrations.transport import ProviderHTTP
from app.llm.models import LLMResponse, ToolCall
from app.security.privacy import ToolResultPrivacy
from app.tools.integrations.register import register
from app.tools.integrations.schemas import CalendarWindowInput, SendEmailInput
from app.tools.registry import ToolRegistry


class MemoryCredentials:
    def __init__(self):
        self.values = {}

    def load(self):
        return {key: value.model_copy(deep=True) for key, value in self.values.items()}

    def save(self, values):
        self.values = {key: value.model_copy(deep=True) for key, value in values.items()}


def settings(**kwargs):
    return Settings(
        _env_file=None,
        integrations_enabled=True,
        google_client_id="google-client",
        slack_client_id="slack-client",
        spotify_client_id="spotify-client",
        api_token="test-api",
        **kwargs,
    )


def account(provider="gmail", scopes=None, expires_at=None):
    spec = PROVIDERS[provider]
    return Account(
        account_id=provider + ":user@example.com",
        provider=provider,
        identity="user@example.com",
        scopes=list(spec.read_scopes + spec.write_scopes) if scopes is None else scopes,
        client_id=("google" if provider in {"gmail", "google_calendar"} else provider) + "-client",
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=expires_at or time.time() + 3600,
    )


def manager(handler, provider="gmail", **kwargs):
    store = MemoryCredentials()
    value = account(provider, **kwargs)
    store.values[value.account_id] = value
    return (
        AccountManager(settings(), store, ProviderHTTP(httpx.MockTransport(handler))),
        store,
        value,
    )


@pytest.mark.parametrize("provider", list(PROVIDERS))
async def test_oauth_pkce_provider_identity_and_single_use(provider):
    requests = []
    oauth_scopes = list(PROVIDERS[provider].read_scopes)

    def handler(req):
        requests.append(req)
        if str(req.url) == PROVIDERS[provider].token_url:
            token = {
                "access_token": "new-secret",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "scope": " ".join(oauth_scopes),
            }
            return httpx.Response(
                200, json={"ok": True, "authed_user": token} if provider == "slack" else token
            )
        data = {
            "emailAddress": "me@example.com",
            "email": "me@example.com",
            "id": "listener",
            "team_id": "T12345678",
            "user_id": "U12345678",
        }
        return httpx.Response(200, json=data)

    store = MemoryCredentials()
    service = AccountManager(settings(), store, ProviderHTTP(httpx.MockTransport(handler)))
    started = await service.begin(provider, False)
    query = parse_qs(urlparse(started["authorization_url"]).query)
    state = query["state"][0]
    pending = service.pending[state]
    assert query["code_challenge"][0] == base64.urlsafe_b64encode(
        hashlib.sha256(pending.verifier.encode()).digest()
    ).decode().rstrip("=")
    assert pending.verifier not in repr(pending)
    assert "client_secret" not in query
    assert not set(PROVIDERS[provider].write_scopes).intersection(pending.scopes)
    if provider == "slack":
        assert "user_scope" in query and "scope" not in query
        assert "localhost:8000" in query["redirect_uri"][0]
    with pytest.raises(IntegrationError):
        await service.finish("spotify" if provider != "spotify" else "gmail", state, "code")
    assert requests == []
    connected = await service.finish(provider, state, "authorization-code")
    assert connected["provider"] == provider
    assert "secret" not in json.dumps(connected)
    exchanged = parse_qs(requests[0].content.decode())
    assert exchanged["code_verifier"] == [pending.verifier]
    assert list(store.values.values())[0].access_token.get_secret_value() == "new-secret"
    with pytest.raises(IntegrationError):
        await service.finish(provider, state, "authorization-code")
    assert len(requests) == 2


async def test_state_expiry_denial_and_unknown_state_make_no_requests():
    http = SimpleNamespace(request=AsyncMock())
    service = AccountManager(settings(), MemoryCredentials(), http)
    with pytest.raises(IntegrationError):
        await service.finish("gmail", "unknown", "code")
    await service.begin("gmail", False)
    state = next(iter(service.pending))
    service.pending[state].expires_at = 0
    with pytest.raises(IntegrationError):
        await service.finish("gmail", state, "code")
    await service.begin("gmail", False)
    state = next(iter(service.pending))
    with pytest.raises(IntegrationError):
        await service.finish("gmail", state, None, "access_denied")
    assert state not in service.pending
    http.request.assert_not_called()


async def test_refresh_rotation_is_saved_and_grants_rechecked():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(
            200,
            json={
                "access_token": "rotated",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
                "scope": GOOGLE + "gmail.readonly",
            },
        )

    service, store, value = manager(handler, expires_at=1)
    with pytest.raises(IntegrationError, match="permissions changed"):
        await service.request(
            value.account_id,
            "gmail",
            "POST",
            "messages/send",
            scopes=(GOOGLE + "gmail.send",),
            json={},
        )
    assert len(seen) == 1
    assert store.values[value.account_id].refresh_token.get_secret_value() == "rotated-refresh"


async def test_readonly_wrong_provider_and_disconnect_fail_before_network():
    http = SimpleNamespace(request=AsyncMock())
    store = MemoryCredentials()
    value = account(scopes=[GOOGLE + "gmail.readonly"])
    store.values[value.account_id] = value
    service = AccountManager(settings(), store, http)
    for provider, scopes in [("gmail", (GOOGLE + "gmail.send",)), ("slack", ())]:
        with pytest.raises(IntegrationError):
            await service.request(
                value.account_id, provider, "POST", "messages/send", scopes=scopes
            )
    catalog = await service.catalog()
    assert "access-secret" not in json.dumps(catalog)
    await service.disconnect(value.account_id)
    with pytest.raises(IntegrationError):
        await service.request(value.account_id, "gmail", "GET", "profile")
    http.request.assert_not_called()


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
async def test_provider_errors_redacted_and_never_retried(status):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            status,
            headers={"Location": "https://evil.example/"},
            json={"error": "access-secret provider-body"},
        )

    service, _, value = manager(handler)
    with pytest.raises(IntegrationError) as caught:
        await service.request(
            value.account_id,
            "gmail",
            "POST",
            "messages/send",
            scopes=(GOOGLE + "gmail.send",),
            json={},
        )
    assert "access-secret" not in str(caught.value)
    assert "provider-body" not in str(caught.value)
    assert len(calls) == 1


async def test_timeout_reports_uncertain_write_without_retry():
    calls = []

    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout("access-secret", request=req)

    service, _, value = manager(handler)
    with pytest.raises(IntegrationError, match="may have succeeded"):
        await service.request(value.account_id, "gmail", "POST", "messages/send")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "name,provider,arguments",
    [
        (
            "email_send",
            "gmail",
            {"to": ["alex@example.com"], "subject": "Hello", "body": "Private body"},
        ),
        ("slack_send_message", "slack", {"channel_id": "C12345678", "text": "Private message"}),
        (
            "calendar_create_event",
            "google_calendar",
            {
                "title": "Planning",
                "start": "2026-10-01T09:00:00+02:00",
                "end": "2026-10-01T10:00:00+02:00",
                "attendees": ["alex@example.com"],
            },
        ),
    ],
)
async def test_writes_require_confirmation_and_send_exact_content(name, provider, arguments):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            200,
            json={"ok": True, "id": "receipt", "ts": "1234567890.123456", "channel": "C12345678"},
        )

    service, _, value = manager(handler, provider)
    registry = ToolRegistry()
    register(registry, service)
    call = ToolCall(
        call_id="test", name=name, arguments={"account_id": value.account_id, **arguments}
    )
    executor = Executor(registry)
    result = await executor.execute(call, "request")
    assert result["status"] == "confirmation_required"
    assert calls == []
    assert not registry.get(name).persist_arguments
    result = await executor.execute(call, "request", approved=True)
    assert result["success"]
    assert len(calls) == 1
    sent = json.loads(calls[0].content)
    if provider == "gmail":
        message = message_from_bytes(base64.urlsafe_b64decode(sent["raw"]))
        assert message["To"] == "alex@example.com"
        assert message["Subject"] == "Hello"
        assert "Private body" in message.get_payload()
    elif provider == "slack":
        assert sent["text"] == "Private message"
        assert sent["channel"] == arguments["channel_id"]
        assert not sent["mrkdwn"] and not sent["unfurl_links"]
    else:
        assert sent["attendees"] == [{"email": "alex@example.com"}]
        assert calls[0].url.params["sendUpdates"] == "all"
        assert sent["start"] == {"dateTime": arguments["start"]}


async def test_approval_once_decline_and_private_journal(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"id": "receipt"})

    service, _, value = manager(handler)
    llm = AsyncMock()
    llm.generate_response.return_value = LLMResponse(text="Sent")
    agent = build_agent(
        settings(database_path=tmp_path / "db"), llm=llm, runner=AsyncMock(), accounts=service
    )
    try:
        args = {
            "account_id": value.account_id,
            "to": ["alex@example.com"],
            "subject": "Private subject",
            "body": "Private body",
        }
        result = await agent.request_tool("email_send", args)
        snapshot = json.dumps(agent.get_workflow(result["request_id"]))
        assert "Private body" not in snapshot and "alex@example.com" not in snapshot
        token = result["confirmation"]["token"]
        declined = await agent.confirm(token, False)
        assert declined["status"] == "cancelled" and not calls
        with pytest.raises(ValueError):
            await agent.confirm(token, True)
        result = await agent.request_tool("email_send", args)
        await agent.confirm(result["confirmation"]["token"], True)
        assert len(calls) == 1
        with pytest.raises(ValueError):
            await agent.confirm(result["confirmation"]["token"], True)
    finally:
        await agent.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"to": ["alex@example.com\r\nBcc: evil@example.com"]},
        {"subject": "Hello\nBcc: evil@example.com"},
        {"thread_id": "123abc"},
    ],
)
def test_email_rejects_header_injection_and_incomplete_replies(changes):
    data = {
        "account_id": "gmail:me@example.com",
        "to": ["alex@example.com"],
        "subject": "Hello",
        "body": "Hi",
        **changes,
    }
    with pytest.raises(ValidationError):
        SendEmailInput.model_validate(data)


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-10-01T09:00:00", "2026-10-01T10:00:00"),
        ("2026-10-01T10:00:00Z", "2026-10-01T09:00:00Z"),
    ],
)
def test_calendar_requires_unambiguous_increasing_window(start, end):
    with pytest.raises(ValidationError):
        CalendarWindowInput(account_id="google_calendar:me@example.com", start=start, end=end)


def test_email_html_is_plain_text_no_external_assets_and_attachments_ignored():
    html = '<p>Hello Alex</p><script>bad()</script><img src="https://tracking.example/pixel">'
    data = {
        "id": "abc",
        "payload": {
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()},
                },
                {
                    "filename": "secret.txt",
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(b"attachment secret").decode()},
                },
            ]
        },
    }
    result = message_view(data)
    assert "Hello Alex" in result["body"]
    assert "bad()" not in result["body"] and "attachment secret" not in result["body"]
    assert "tracking.example" not in result["body"]


async def test_spotify_uses_exact_uri_and_device_and_returns_real_success():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(204)

    service, _, value = manager(handler, "spotify")
    registry = ToolRegistry()
    register(registry, service)
    uri = "spotify:playlist:" + "a" * 22
    result = await Executor(registry).execute(
        ToolCall(
            call_id="play",
            name="spotify_play",
            arguments={"account_id": value.account_id, "uri": uri, "device_id": "device-1"},
        ),
        "request",
    )
    assert result["success"]
    assert json.loads(calls[0].content) == {"context_uri": uri}
    assert calls[0].url.params["device_id"] == "device-1"


def test_connected_content_follows_existing_remote_privacy_policy():
    result = {
        "tool": "email_read_thread",
        "success": True,
        "status": "completed",
        "result": {"body": "private thread"},
    }
    assert "private thread" not in json.dumps(
        ToolResultPrivacy(provider="openai").filter_result(result)
    )
    assert "private thread" in json.dumps(
        ToolResultPrivacy(provider="ollama").filter_result(result)
    )


def test_keychain_uses_secret_values_only_in_explicit_storage(monkeypatch):
    backend = SimpleNamespace(
        get_password=lambda *args: None, set_password=lambda *args: written.append(args)
    )
    written = []
    store = KeychainCredentials()
    monkeypatch.setattr(store, "_backend", lambda: backend)
    value = account()
    store.save({value.account_id: value})
    assert "access-secret" in written[0][2]
    assert "access-secret" not in repr(value)
    assert "access-secret" not in json.dumps(value.public())
    backend.get_password = lambda *args: written[0][2]
    assert store.load()[value.account_id].access_token.get_secret_value() == "access-secret"


def test_connection_routes_auth_csrf_and_callback_state():
    http = SimpleNamespace(request=AsyncMock())
    service = AccountManager(settings(), MemoryCredentials(), http)
    agent = SimpleNamespace(accounts=service, close=AsyncMock())
    auth = {"Authorization": "Bearer test-api", "Origin": "http://127.0.0.1:8000"}
    with TestClient(
        create_app(settings(), agent, enable_ui=True), base_url="http://127.0.0.1:8000"
    ) as client:
        assert client.get("/api/v1/connections").status_code == 401
        assert (
            client.post(
                "/api/v1/connections/start",
                headers={**auth, "Origin": "https://evil.example"},
                json={"provider": "gmail"},
            ).status_code
            == 403
        )
        response = client.post(
            "/api/v1/connections/start", headers=auth, json={"provider": "gmail"}
        )
        assert response.status_code == 200
        callback = client.get("/api/v1/connections/callback/gmail?state=unknown&code=secret-code")
        assert callback.status_code == 400
        assert "secret-code" not in callback.text
        assert callback.headers["cache-control"] == "no-store"
        assert callback.headers["referrer-policy"] == "no-referrer"
        http.request.assert_not_called()
