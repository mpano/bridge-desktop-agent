"""Owner sign-up and sign-in. Google/GitHub are mocked; no real accounts are touched."""

import asyncio
import time
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.launch import LaunchTickets
from app.api.server import create_app
from app.auth.store import AuthStore, hash_password, verify_password
from app.config.settings import Settings

ORIGIN = {"origin": "http://localhost:8000"}
PASSWORD = "correct horse battery"


def make_client(tmp_path, **settings):
    tickets = LaunchTickets()
    config = Settings(
        _env_file=None,
        api_token="api-secret",
        database_path=tmp_path / "db",
        google_client_id="google-client",
        google_client_secret="google-secret",
        github_client_id="github-client",
        github_client_secret="github-secret",
        **settings,
    )
    agent = Mock(scheduler=None, lock=asyncio.Lock(), close=AsyncMock())
    agent.capabilities.return_value = []
    app = create_app(config, agent, enable_ui=True, launch_tickets=tickets)
    client = TestClient(app, base_url="http://localhost:8000")
    return client, tickets, app


def setup_session(client, tickets):
    response = client.post(
        "/api/v1/session/launch", json={"ticket": tickets.issue()}, headers=ORIGIN
    )
    assert response.json()["setup"] is True


def sign_up(client, tickets):
    setup_session(client, tickets)
    response = client.post(
        "/api/v1/auth/signup",
        json={"name": "Mpano", "email": "me@example.com", "password": PASSWORD},
        headers=ORIGIN,
    )
    assert response.status_code == 200, response.text
    return response


def test_passwords_are_salted_scrypt_hashes():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second and first.startswith("scrypt$") and PASSWORD not in first
    assert verify_password(PASSWORD, first) and not verify_password("wrong", first)
    assert not verify_password(PASSWORD, None) and not verify_password(PASSWORD, "garbage")


def test_only_the_macs_user_can_create_the_owner_account(tmp_path):
    client, tickets, _ = make_client(tmp_path)
    with client:
        status = client.get("/api/v1/auth/status").json()
        assert status["has_owner"] is False and status["signed_in"] is False
        body = {"name": "Intruder", "email": "x@example.com", "password": PASSWORD}
        refused = client.post("/api/v1/auth/signup", json=body, headers=ORIGIN)
        assert refused.status_code == 403
        sign_up(client, tickets)
        status = client.get("/api/v1/auth/status").json()
        assert status["signed_in"] and status["owner"]["email"] == "me@example.com"
        again = client.post("/api/v1/auth/signup", json=body, headers=ORIGIN)
        assert again.status_code in {403, 409}


def test_session_cookie_unlocks_the_api_and_state_changes_need_the_page_origin(tmp_path):
    client, tickets, _ = make_client(tmp_path)
    with client:
        assert client.get("/api/v1/capabilities").status_code == 401
        sign_up(client, tickets)
        assert client.get("/api/v1/capabilities").status_code == 200
        blocked = client.post("/api/v1/conversation/reset")
        assert blocked.status_code == 403
        assert client.post("/api/v1/conversation/reset", headers=ORIGIN).status_code == 200
        evil = client.get("/api/v1/capabilities", headers={"origin": "http://evil.example"})
        assert evil.status_code == 403
        client.post("/api/v1/auth/logout", headers=ORIGIN)
        assert client.get("/api/v1/capabilities").status_code == 401
        # The menu bar and voice keep working with the API token.
        token = {"Authorization": "Bearer api-secret"}
        assert client.get("/api/v1/capabilities", headers=token).status_code == 200


def test_password_login_remember_me_and_lockout(tmp_path):
    client, tickets, _ = make_client(tmp_path)
    with client:
        sign_up(client, tickets)
        client.post("/api/v1/auth/logout", headers=ORIGIN)
        good = {"email": "ME@example.com", "password": PASSWORD, "remember": True}
        response = client.post("/api/v1/auth/login", json=good, headers=ORIGIN)
        assert response.status_code == 200
        assert "Max-Age=2592000" in response.headers["set-cookie"]
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "samesite=strict" in response.headers["set-cookie"].lower()
        client.post("/api/v1/auth/logout", headers=ORIGIN)
        bad = {"email": "me@example.com", "password": "wrong password"}
        for _ in range(5):
            assert client.post("/api/v1/auth/login", json=bad, headers=ORIGIN).status_code == 401
        locked = client.post("/api/v1/auth/login", json=good, headers=ORIGIN)
        assert locked.status_code == 429


def test_account_sessions_profile_and_password_change(tmp_path):
    client, tickets, _ = make_client(tmp_path)
    with client:
        sign_up(client, tickets)
        other = TestClient(client.app, base_url="http://localhost:8000")
        other.post(
            "/api/v1/auth/login",
            json={"email": "me@example.com", "password": PASSWORD},
            headers=ORIGIN,
        )
        account = client.get("/api/v1/auth/account").json()
        assert len(account["sessions"]) == 2
        assert sum(item["current"] for item in account["sessions"]) == 1
        assert client.post("/api/v1/auth/sessions/end-others", headers=ORIGIN).json()["ended"] == 1
        assert other.get("/api/v1/auth/account").status_code == 401
        wrong = {"current": "nope", "new": "a brand new password"}
        assert client.post("/api/v1/auth/password", json=wrong, headers=ORIGIN).status_code == 401
        right = {"current": PASSWORD, "new": "a brand new password"}
        assert client.post("/api/v1/auth/password", json=right, headers=ORIGIN).status_code == 200
        profile = {"name": "Akim", "email": "akim@example.com"}
        assert client.post("/api/v1/auth/profile", json=profile, headers=ORIGIN).status_code == 200
        assert client.get("/api/v1/auth/status").json()["owner"]["name"] == "Akim"
        # The only sign-in method can't be removed.
        last = client.post("/api/v1/auth/unlink", json={"value": "google"}, headers=ORIGIN)
        assert last.status_code == 409


def provider_transport(subject="google-123", verified=True, github_emails=None):
    def handler(request):
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token") or "access_token" in url:
            return httpx.Response(200, json={"access_token": "provider-access"})
        if "openidconnect" in url:
            return httpx.Response(
                200,
                json={
                    "sub": subject,
                    "email": "me@gmail.com",
                    "email_verified": verified,
                    "name": "Mpano Google",
                },
            )
        if url == "https://api.github.com/user":
            return httpx.Response(200, json={"id": 42, "login": "mpano", "name": "Mpano"})
        if url == "https://api.github.com/user/emails":
            emails = (
                github_emails
                if github_emails is not None
                else [{"email": "me@users.noreply.github.com", "primary": True, "verified": True}]
            )
            return httpx.Response(200, json=emails)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def oauth(client, provider, intent, remember=False):
    started = client.post(
        f"/api/v1/auth/oauth/{provider}/start",
        json={"intent": intent, "remember": remember},
        headers=ORIGIN,
    )
    assert started.status_code == 200, started.text
    query = parse_qs(urlparse(started.json()["url"]).query)
    assert (
        query["redirect_uri"][0] == f"http://localhost:8000/api/v1/auth/oauth/{provider}/callback"
    )
    assert query["code_challenge_method"] == ["S256"]
    return client.get(
        f"/api/v1/auth/oauth/{provider}/callback",
        params={"state": query["state"][0], "code": "provider-code"},
        follow_redirects=False,
    )


def test_sign_up_with_google_then_sign_in_with_it(tmp_path):
    client, tickets, app = make_client(tmp_path)
    app.state.auth.oauth.transport = provider_transport()
    with client:
        setup_session(client, tickets)
        done = oauth(client, "google", "signup")
        assert done.status_code == 303 and done.headers["location"] == "/"
        status = client.get("/api/v1/auth/status").json()
        assert status["signed_in"] and status["owner"]["email"] == "me@gmail.com"
        assert status["methods"]["google"]["linked"]
        client.post("/api/v1/auth/logout", headers=ORIGIN)
        again = oauth(client, "google", "login")
        assert again.headers["location"] == "/"
        assert client.get("/api/v1/auth/status").json()["signed_in"]


def test_unlinked_or_unverified_social_accounts_cannot_sign_in(tmp_path):
    client, tickets, app = make_client(tmp_path)
    with client:
        sign_up(client, tickets)
        client.post("/api/v1/auth/logout", headers=ORIGIN)
        app.state.auth.oauth.transport = provider_transport()
        denied = oauth(client, "github", "login")
        assert denied.headers["location"].startswith("/#auth-error=")
        assert (
            "isn%E2%80%99t%20linked" in denied.headers["location"]
            or "linked" in denied.headers["location"]
        )
        assert not client.get("/api/v1/auth/status").json()["signed_in"]
        app.state.auth.oauth.transport = provider_transport(verified=False)
        unverified = oauth(client, "google", "login")
        assert "verified" in unverified.headers["location"]
        # A stolen or replayed state is refused.
        replay = client.get(
            "/api/v1/auth/oauth/google/callback",
            params={"state": "made-up", "code": "x"},
            follow_redirects=False,
        )
        assert "expired" in replay.headers["location"]


def test_link_github_while_signed_in_then_unlink(tmp_path):
    client, tickets, app = make_client(tmp_path)
    app.state.auth.oauth.transport = provider_transport()
    with client:
        signed_out = client.post(
            "/api/v1/auth/oauth/github/start", json={"intent": "link"}, headers=ORIGIN
        )
        assert signed_out.status_code in {401, 409}
        sign_up(client, tickets)
        linked = oauth(client, "github", "link")
        assert linked.headers["location"] == "/#account-linked=github"
        account = client.get("/api/v1/auth/account").json()
        assert account["identities"][0]["login"] == "mpano"
        assert client.post("/api/v1/auth/unlink", json={"value": "github"}, headers=ORIGIN).json()


def test_passkeys_need_localhost_and_a_registered_credential(tmp_path):
    client, tickets, _ = make_client(tmp_path)
    with client:
        assert client.post("/api/v1/auth/passkeys/options", headers=ORIGIN).status_code == 401
        sign_up(client, tickets)
        options = client.post("/api/v1/auth/passkeys/options", headers=ORIGIN).json()
        assert options["rp"]["id"] == "localhost"
        assert options["authenticatorSelection"]["userVerification"] == "required"
        none_yet = client.post("/api/v1/auth/passkey-login/options", headers=ORIGIN)
        assert none_yet.status_code == 409
        forged = client.post(
            "/api/v1/auth/passkeys",
            json={"credential": {"id": "x", "rawId": "x", "type": "public-key", "response": {}}},
            headers=ORIGIN,
        )
        assert forged.status_code == 400


def test_sessions_expire(tmp_path):
    store = AuthStore(tmp_path / "db")
    token = store.create_session("owner", hours=0.0001)
    assert store.session(token) is not None
    time.sleep(0.5)
    assert store.session(token) is None
    assert store.session(None) is None


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "x y@example.com"])
def test_sign_up_rejects_invalid_email(tmp_path, email):
    client, tickets, _ = make_client(tmp_path)
    with client:
        setup_session(client, tickets)
        response = client.post(
            "/api/v1/auth/signup",
            json={"name": "Me", "email": email, "password": PASSWORD},
            headers=ORIGIN,
        )
        assert response.status_code == 422
