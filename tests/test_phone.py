"""Bridge on your phone: Tailscale-only access, pairing, phone sessions, pushes."""

import asyncio
import json
import sys
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.llm.models import LLMResponse, ToolCall
from app.phone import tailscale
from app.phone.access import PhoneAccess, phone_may_use
from app.phone.webpush import Vapid, allowed_endpoint, b64, encrypt, unb64

HOST, LOGIN = "mac.tail-test.ts.net", "me@example.test"
TOKEN = {"Authorization": "Bearer token"}
PHONE = {"Host": HOST, "Tailscale-User-Login": LOGIN, "Origin": f"https://{HOST}"}


# Web Push ------------------------------------------------------------------------------------


def test_encryption_matches_rfc_8291_example():
    """RFC 8291 Appendix A: the same keys and salt give the same message, byte for byte."""
    sender = ec.derive_private_key(
        int.from_bytes(unb64("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
        ec.SECP256R1(),
    )
    body = encrypt(
        b"When I grow up, I want to be a watermelon",
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
        sender=sender,
        salt=unb64("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    assert b64(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLoc"
        "InmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWL"
        "VWGNWQexSgSxsj_Qulcy4a-fN"
    )


def test_vapid_header_is_a_signed_jwt_for_the_push_service():
    vapid = Vapid()
    header = vapid.header("https://web.push.apple.com/abc", f"https://{HOST}", now=1000)
    token = header.split("t=")[1].split(",")[0]
    assert header.endswith(f"k={vapid.public_key}")
    head, claims, signature = token.split(".")
    assert json.loads(unb64(claims)) == {
        "aud": "https://web.push.apple.com",
        "exp": 1000 + 12 * 3600,
        "sub": f"https://{HOST}",
    }
    raw = unb64(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    vapid.key.public_key().verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert Vapid(vapid.pem()).public_key == vapid.public_key


def test_only_real_push_services_are_accepted():
    assert allowed_endpoint("https://web.push.apple.com/QGx1")
    assert allowed_endpoint("https://fcm.googleapis.com/fcm/send/x")
    assert not allowed_endpoint("http://web.push.apple.com/x")
    assert not allowed_endpoint("https://evil.example/web.push.apple.com")
    assert not allowed_endpoint("https://web.push.apple.com.evil.example/x")
    assert not allowed_endpoint("https://127.0.0.1:8000/api/v1/tasks")


# Tailscale -----------------------------------------------------------------------------------


def test_tailscale_status_and_serve_state():
    status = tailscale.parse_status(
        {
            "BackendState": "Running",
            "Self": {"DNSName": "Mac.tail-test.ts.net.", "UserID": 7},
            "User": {"7": {"LoginName": LOGIN}},
            "CertDomains": ["mac.tail-test.ts.net"],
            "Peer": {
                "a": {
                    "HostName": "localhost",
                    "DNSName": "iphone.tail-test.ts.net.",
                    "OS": "iOS",
                    "Online": True,
                },
                "b": {"HostName": "server", "OS": "linux", "Online": True},
            },
        }
    )
    assert status == {
        "installed": True,
        "running": True,
        "host": HOST,
        "https": True,
        "login": LOGIN,
        "phones": [{"name": "iphone", "os": "iOS", "online": True}],
    }
    ours = {"Web": {f"{HOST}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}}}}
    assert tailscale.serving(ours, HOST, 8000) == "ours"
    assert tailscale.serving({}, HOST, 8000) == "none"
    assert tailscale.serving({"TCP": {"443": {"HTTPS": True}}}, HOST, 8000) == "other"
    other = {"Web": {f"{HOST}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}
    assert tailscale.serving(other, HOST, 8000) == "other"


# Phone access ------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def request(host=HOST, login=LOGIN):
    return httpx.Request(
        "GET", "http://127.0.0.1:8000/", headers={"host": host, "tailscale-user-login": login}
    )


def test_only_your_own_tailscale_login_on_bridges_address_counts_as_the_phone(tmp_path):
    access = PhoneAccess(tmp_path / "db")
    assert not access.is_phone(request())
    access.configure(HOST, LOGIN)
    assert access.is_phone(request())
    assert not access.is_phone(request(login="someone@else.test"))
    assert not access.is_phone(request(login=""))
    assert not access.is_phone(request(host="localhost:8000"))
    access.disable()
    assert not access.is_phone(request())


def test_pairing_codes_work_once_and_expire(tmp_path):
    clock = Clock()
    access = PhoneAccess(tmp_path / "db", clock=clock)
    ticket = access.pair_ticket()
    assert access.redeem(ticket)
    assert not access.redeem(ticket)
    late = access.pair_ticket()
    clock.now += 301
    assert not access.redeem(late)


def test_phone_may_use_only_its_screens():
    assert phone_may_use("GET", "/api/v1/today")
    assert phone_may_use("POST", "/api/v1/inbox/send")
    assert phone_may_use("POST", "/api/v1/tasks/confirm")
    assert phone_may_use("GET", "/ui/app.js")
    for method, path in [
        ("GET", "/api/v1/settings/app"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/signup"),
        ("POST", "/api/v1/session/launch"),
        ("POST", "/api/v1/text/transform"),
        ("GET", "/api/v1/screen/peek"),
        ("GET", "/api/v1/memories"),
        ("POST", "/api/v1/phone/enable"),
        ("POST", "/api/v1/chats/delete"),
    ]:
        assert not phone_may_use(method, path), path


def subscription():
    receiver = ec.generate_private_key(ec.SECP256R1())
    key = b64(receiver.public_key().public_bytes(*_uncompressed()))
    return key, b64(b"0123456789abcdef")


def _uncompressed():
    from cryptography.hazmat.primitives import serialization

    return serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint


def test_notifications_go_to_the_phone_only_when_away_and_gone_phones_are_forgotten(
    tmp_path, monkeypatch
):
    away = {"value": False}
    access = PhoneAccess(tmp_path / "db", away=lambda: away["value"])
    access.configure(HOST, LOGIN)
    p256dh, auth = subscription()
    access.subscribe("https://web.push.apple.com/one", p256dh, auth)
    with pytest.raises(ValueError):
        access.subscribe("https://evil.example/push", p256dh, auth)
    sent = []

    def handler(req):
        sent.append(req)
        return httpx.Response(410 if len(sent) > 1 else 201)

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))

    assert asyncio.run(access.notify("Bridge", "At your Mac")) == 0  # You're at the Mac.
    away["value"] = True
    assert asyncio.run(access.notify("Bridge", "Needs your OK", "chat")) == 1
    pushed = sent[0]
    assert pushed.headers["Content-Encoding"] == "aes128gcm"
    assert pushed.headers["Authorization"].startswith("vapid t=")
    assert b"Needs your OK" not in pushed.content  # Encrypted for the phone only.
    access.set_notify_mode("always")
    away["value"] = False
    assert asyncio.run(access.notify("Bridge", "Again")) == 0  # 410: the phone app is gone.
    assert access.subscriptions() == []


# The API, as Tailscale delivers it -----------------------------------------------------------


def make(tmp_path, *replies):
    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    llm = AsyncMock()
    llm.generate_response.side_effect = list(replies)
    agent = build_agent(settings, llm=llm, runner=AsyncMock())
    agent.scheduler = None
    agent.phone.configure(HOST, LOGIN)
    agent.phone.away = lambda: True
    app = create_app(settings, agent, enable_ui=True)
    from app.auth.store import AuthStore

    AuthStore(settings.database_path).create_owner(LOGIN, "Me", None)
    return agent, app


def pair(client) -> str:
    code = client.post("/api/v1/phone/pair-code", headers=TOKEN).json()
    ticket = code["url"].split("#pair=")[1]
    assert code["url"].startswith(f"https://{HOST}/#pair=")
    paired = client.post("/api/v1/phone/pair", json={"ticket": ticket}, headers=PHONE)
    assert paired.status_code == 200
    cookie = paired.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Max-Age=86400" in cookie  # 24 hours, then Face ID.
    again = client.post("/api/v1/phone/pair", json={"ticket": ticket}, headers=PHONE)
    assert again.status_code == 401
    return cookie.split(";")[0]


def test_phone_pairs_with_the_qr_code_and_reaches_only_its_screens(tmp_path):
    agent, app = make(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/v1/approvals", headers={"Host": "evil.ts.net"}).status_code == 400
        stranger = {**PHONE, "Tailscale-User-Login": "someone@else.test"}
        assert client.get("/", headers=stranger).status_code == 403
        assert client.get("/", headers={"Host": HOST}).status_code == 403
        relayed = {"Tailscale-User-Login": LOGIN, "X-Forwarded-Host": HOST}
        assert client.get("/api/v1/approvals", headers={**relayed, **TOKEN}).status_code == 400

        status = client.get("/api/v1/auth/status", headers=PHONE).json()
        assert status["phone"] and not status["signed_in"] and "password" not in status["methods"]
        assert client.get("/api/v1/approvals", headers=PHONE).status_code == 401
        # The API token is for this Mac's own apps, not the phone.
        assert client.get("/api/v1/approvals", headers={**PHONE, **TOKEN}).status_code == 401
        # Pairing is for the phone; the code comes from the Mac.
        assert client.post("/api/v1/phone/pair-code", headers={**PHONE, **TOKEN}).status_code == 403
        assert client.post("/api/v1/phone/pair", json={"ticket": "x" * 20}).status_code == 403

        session = {**PHONE, "Cookie": pair(client)}
        assert client.get("/api/v1/auth/status", headers=session).json()["signed_in"]
        assert client.get("/api/v1/approvals", headers=session).status_code == 200
        me = client.get("/api/v1/phone/me", headers=session).json()
        assert len(unb64(me["vapid_public_key"])) == 65 and me["face_id"] is False
        blocked = client.get("/api/v1/settings/app", headers=session)
        assert blocked.status_code == 403 and "Mac" in blocked.json()["detail"]
        assert client.post("/api/v1/auth/login", json={}, headers=session).status_code == 403
        # A phone session is no good on the Mac's own address.
        assert (
            client.get("/api/v1/approvals", headers={"Cookie": session["Cookie"]}).status_code
            == 401
        )

        p256dh, auth = subscription()
        bad = {"endpoint": "https://evil.example/x", "keys": {"p256dh": p256dh, "auth": auth}}
        assert client.post("/api/v1/phone/push", json=bad, headers=session).status_code == 422
        good = {
            "endpoint": "https://web.push.apple.com/abc",
            "keys": {"p256dh": p256dh, "auth": auth},
        }
        assert client.post("/api/v1/phone/push", json=good, headers=session).status_code == 200
        state = client.get("/api/v1/phone", headers=TOKEN).json()
        assert state["notifications"] == 1 and len(state["sessions"]) == 1

        signed_out = client.post("/api/v1/phone/sign-out", headers=TOKEN).json()
        assert signed_out["sessions"] == [] and signed_out["notifications"] == 0
        assert client.get("/api/v1/approvals", headers=session).status_code == 401


def test_turning_phone_access_on_and_off(tmp_path, monkeypatch):
    agent, app = make(tmp_path)
    agent.phone.disable()
    calls = []
    net = {"installed": True, "running": True, "host": HOST, "https": False, "login": LOGIN}
    serve = {"state": "none"}

    async def status():
        return {**net, "phones": []}

    async def serve_state(host, port):
        calls.append(("state", host, port))
        return serve["state"]

    async def serve_on(port):
        calls.append(("on", port))
        serve["state"] = "ours"

    async def serve_off():
        calls.append(("off",))
        serve["state"] = "none"

    monkeypatch.setattr(tailscale, "status", status)
    monkeypatch.setattr(tailscale, "serve_state", serve_state)
    monkeypatch.setattr(tailscale, "serve_on", serve_on)
    monkeypatch.setattr(tailscale, "serve_off", serve_off)
    with TestClient(app) as client:
        refused = client.post("/api/v1/phone/enable", headers=TOKEN)
        assert refused.status_code == 409 and "HTTPS" in refused.json()["detail"]
        net["https"] = True
        serve["state"] = "other"
        assert client.post("/api/v1/phone/enable", headers=TOKEN).status_code == 409
        serve["state"] = "none"
        on = client.post("/api/v1/phone/enable", headers=TOKEN).json()
        assert on["enabled"] and on["serving"] and on["url"] == f"https://{HOST}"
        assert ("on", 8000) in calls
        assert agent.phone.is_phone(request())
        session = {**PHONE, "Cookie": pair(client)}
        off = client.post("/api/v1/phone/disable", headers=TOKEN).json()
        assert not off["enabled"] and ("off",) in calls
        assert client.get("/api/v1/approvals", headers=session).status_code == 400


def test_from_the_phone_bridge_wont_read_the_macs_screen(tmp_path):
    agent, app = make(
        tmp_path,
        LLMResponse(calls=[ToolCall(call_id="1", name="screen_context", arguments={})]),
    )
    with TestClient(app) as client:
        session = {**PHONE, "Cookie": pair(client)}
        started = client.post("/api/v1/tasks", json={"message": "What's this?"}, headers=session)
        request_id = started.json()["request_id"]
        for _ in range(200):
            progress = client.get(f"/api/v1/tasks/{request_id}", headers=session).json()
            if progress["result"]:
                break
        assert progress["result"]["status"] == "failed"
        assert "from your Mac" in progress["result"]["message"]
        assert progress["steps"] == []


@pytest.mark.skipif(sys.platform != "darwin", reason="Core Image draws the QR code")
def test_qr_code_is_drawn_by_macos():
    from app.phone.qr import matrix

    rows = matrix(f"https://{HOST}/#pair=" + "x" * 32)
    assert rows and len(rows) == len(rows[0]) and set("".join(rows)) == {"0", "1"}
    assert rows[1].startswith("01111111")  # The top-left finder pattern, after the margin.


def test_icons_manifest_and_service_worker_are_served(tmp_path):
    agent, app = make(tmp_path)
    with TestClient(app) as client:
        manifest = client.get("/manifest.webmanifest")
        assert manifest.headers["content-type"].startswith("application/manifest+json")
        assert manifest.json()["display"] == "standalone"
        assert client.get("/sw.js").headers["content-type"].startswith("text/javascript")
        for size in (180, 192, 512):
            icon = client.get(f"/ui/icon-{size}.png")
            assert icon.status_code == 200 and icon.content[:4] == b"\x89PNG"
        policy = client.get("/").headers["content-security-policy"]
        assert "manifest-src 'self'" in policy and "worker-src 'self'" in policy
        # The phone gets the page too, through Tailscale.
        assert client.get("/", headers=PHONE).status_code == 200
        assert client.get("/sw.js", headers=PHONE).status_code == 200


def test_tailscale_cli_gets_a_terminal_type_when_bridge_has_none(monkeypatch, tmp_path):
    """Opened from Finder, Bridge has no TERM; without one Tailscale's app opens its window."""
    script = tmp_path / "tailscale"
    script.write_text(
        '#!/bin/sh\n[ -n "$TERM" ] && echo cli || echo "The Tailscale GUI failed to start"\n'
    )
    script.chmod(0o755)
    monkeypatch.setattr(tailscale, "CANDIDATES", (str(script),))
    monkeypatch.delenv("TERM", raising=False)
    assert asyncio.run(tailscale.run("version")).strip() == "cli"
