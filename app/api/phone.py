"""Routes for Bridge on your phone.

On the Mac (Settings › Phone): check Tailscale, turn phone access on or off, show the QR
code that signs the phone in, and choose when the phone is notified. On the phone: redeem
that QR code, and subscribe to notifications.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.phone import tailscale
from app.phone.access import PhoneAccess
from app.phone.qr import matrix
from app.phone.tailscale import TailscaleError
from app.phone.webpush import unb64


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Pair(Body):
    ticket: str = Field(min_length=16, max_length=100)


class PushKeys(Body):
    p256dh: str = Field(min_length=80, max_length=120)
    auth: str = Field(min_length=16, max_length=40)


class Subscription(Body):
    endpoint: str = Field(min_length=10, max_length=1000)
    keys: PushKeys
    expirationTime: float | None = None  # noqa: N815 — the browser's own field name.


class Endpoint(Body):
    endpoint: str = Field(min_length=10, max_length=1000)


class PhoneSettings(Body):
    notify: Literal["away", "always"]


def install(app: FastAPI, authorize, settings, auth, from_phone) -> None:
    def phone(request: Request):
        access = getattr(request.app.state.agent, "phone", None)
        if not isinstance(access, PhoneAccess):
            raise HTTPException(404, "Phone access isn't available.")
        return access

    def on_mac(request: Request):
        if from_phone(request):
            raise HTTPException(403, "Open Bridge on your Mac for this.")

    def on_phone(request: Request):
        if not from_phone(request):
            raise HTTPException(403, "This is for Bridge on your phone.")

    def port(request: Request) -> int:
        # Bridge's own port, as this Mac's page reached it (http://localhost:8000).
        return request.url.port or 8000

    def phone_sessions():
        return auth.store.sessions("phone") if auth is not None else []

    async def state(request: Request) -> dict:
        access = phone(request)
        net = await tailscale.status()
        serving = (
            await tailscale.serve_state(access.host, port(request))
            if access.enabled and net.get("running")
            else "none"
        )
        return {
            "tailscale": net,
            "enabled": access.enabled,
            "serving": serving == "ours",
            "url": access.origin,
            "admin_url": tailscale.ADMIN_DNS,
            "notify": access.notify_mode,
            "notifications": len(access.subscriptions()),
            "sessions": [
                {"id": item.id, "last_seen": item.last_seen, "user_agent": item.user_agent}
                for item in phone_sessions()
            ],
            "passkeys": [
                {key: item[key] for key in ("credential_id", "name", "created_at", "last_used")}
                for item in (auth.store.passkeys(access.host) if auth and access.host else [])
            ],
        }

    # On the Mac ------------------------------------------------------------------------------

    @app.get("/api/v1/phone", dependencies=[Depends(authorize)])
    async def read(request: Request):
        on_mac(request)
        return await state(request)

    @app.post("/api/v1/phone/enable", dependencies=[Depends(authorize)])
    async def enable(request: Request):
        on_mac(request)
        access = phone(request)
        net = await tailscale.status()
        if not net["installed"]:
            raise HTTPException(409, "Install Tailscale on this Mac first.")
        if not net["running"] or not net["host"]:
            raise HTTPException(409, "Open Tailscale on this Mac and sign in.")
        if not net["https"]:
            raise HTTPException(
                409, "Turn on HTTPS Certificates in Tailscale's admin page (DNS), then try again."
            )
        if not net.get("login"):
            raise HTTPException(409, "Tailscale didn't say who's signed in. Try again.")
        serving = await tailscale.serve_state(net["host"], port(request))
        if serving == "other":
            raise HTTPException(
                409, "Tailscale already shares something else over https on this Mac."
            )
        try:
            if serving == "none":
                await tailscale.serve_on(port(request))
        except TailscaleError as exc:
            raise HTTPException(409, str(exc)) from None
        access.configure(net["host"], net["login"])
        return await state(request)

    @app.post("/api/v1/phone/disable", dependencies=[Depends(authorize)])
    async def disable(request: Request):
        on_mac(request)
        access = phone(request)
        if access.host and await tailscale.serve_state(access.host, port(request)) == "ours":
            try:
                await tailscale.serve_off()
            except TailscaleError as exc:
                raise HTTPException(409, str(exc)) from None
        for item in phone_sessions():
            auth.store.end_session(session_id=item.id)
        access.disable()
        return await state(request)

    @app.post("/api/v1/phone/pair-code", dependencies=[Depends(authorize)])
    async def pair_code(request: Request):
        on_mac(request)
        access = phone(request)
        if not access.enabled:
            raise HTTPException(409, "Turn on phone access first.")
        url = f"{access.origin}/#pair={access.pair_ticket()}"
        return {"url": url, "qr": await asyncio.to_thread(matrix, url), "expires_in": 300}

    @app.post("/api/v1/phone/settings", dependencies=[Depends(authorize)])
    async def phone_settings(payload: PhoneSettings, request: Request):
        on_mac(request)
        phone(request).set_notify_mode(payload.notify)
        return await state(request)

    @app.post("/api/v1/phone/sign-out", dependencies=[Depends(authorize)])
    async def sign_out_phones(request: Request):
        on_mac(request)
        for item in phone_sessions():
            auth.store.end_session(session_id=item.id)
        phone(request).unsubscribe()
        return await state(request)

    # On the phone ----------------------------------------------------------------------------

    @app.post("/api/v1/phone/pair")
    async def pair(payload: Pair, request: Request):
        """The QR code from the Mac: proves you're at your Mac, so it signs the phone in."""
        on_phone(request)
        if auth is None or auth.store.owner() is None:
            raise HTTPException(409, "Finish setting up Bridge on your Mac first.")
        origin = request.headers.get("origin")
        if origin != phone(request).origin:
            raise HTTPException(403, "Open the link from the QR code.")
        auth.check_rate()
        if not phone(request).redeem(payload.ticket):
            auth.failed()
            raise HTTPException(401, "This code expired. Show a new one on your Mac.")
        return auth.sign_in(JSONResponse({"signed_in": True}), request, kind="phone")

    @app.get("/api/v1/phone/me", dependencies=[Depends(authorize)])
    async def me(request: Request):
        on_phone(request)
        access = phone(request)
        return {
            "vapid_public_key": access.vapid().public_key,
            "face_id": bool(auth.store.passkeys(access.host)),
            "notify": access.notify_mode,
        }

    @app.post("/api/v1/phone/push", dependencies=[Depends(authorize)])
    async def subscribe(payload: Subscription, request: Request):
        on_phone(request)
        try:
            if len(unb64(payload.keys.p256dh)) != 65 or len(unb64(payload.keys.auth)) != 16:
                raise ValueError("Those notification keys aren't valid.")
            phone(request).subscribe(payload.endpoint, payload.keys.p256dh, payload.keys.auth)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"subscribed": True}

    @app.post("/api/v1/phone/push/remove", dependencies=[Depends(authorize)])
    async def unsubscribe(payload: Endpoint, request: Request):
        on_phone(request)
        phone(request).unsubscribe(payload.endpoint)
        return {"subscribed": False}

    @app.post("/api/v1/phone/push/test", dependencies=[Depends(authorize)])
    async def test_push(request: Request):
        on_phone(request)
        sent = await phone(request).notify(
            "Bridge", "Notifications work on your phone.", "today", force=True
        )
        if not sent:
            raise HTTPException(502, "The notification didn't go through. Try turning it on again.")
        return {"sent": sent}
