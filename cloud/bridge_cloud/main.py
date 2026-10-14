"""The Bridge Cloud web service (FastAPI).

    uvicorn --factory bridge_cloud.main:create_app --port 8080

For the Mac app:
  POST /v1/devices                 a new Mac: a trial account and its token
  GET  /v1/account                 plan, credit, what's for sale
  POST /v1/checkout {plan}         a Lemon Squeezy checkout link
  POST /v1/portal                  the "manage subscription" link
  POST /v1/link {license_key}      sign this Mac in to the account a license key belongs to
  POST /v1/sign-out
  POST /v1/voice/end {voice_id}    a conversation ended: charge its minutes
  /v1/openai/...                   OpenAI's API, paid from the account's credit
Lemon Squeezy:
  POST /v1/webhooks/lemonsqueezy
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from app.llm.usage import PRICES

from .config import CloudSettings
from .lemon import Lemon, LemonError, signed
from .pricing import allowed, guess_micros, usage_micros
from .store import MICRO, Store, micros

JSON_PATHS = {"responses", "chat/completions"}
OUT_OF_CREDIT = (
    "Your Bridge AI credit has run out. Add credit or upgrade in Bridge › Settings › Plan."
)


def openai_error(status: int, message: str, code: str = "bridge") -> JSONResponse:
    """Errors in OpenAI's shape, so the app shows the message like any other."""
    return JSONResponse(
        {"error": {"message": message, "type": "bridge_cloud", "code": code}}, status_code=status
    )


class NewDevice(BaseModel):
    name: str = Field(default="", max_length=80)


class Checkout(BaseModel):
    plan: str = Field(max_length=40)


class Link(BaseModel):
    license_key: str = Field(min_length=8, max_length=120)


class VoiceEnd(BaseModel):
    voice_id: str = Field(max_length=60)


class Limiter:
    """At most N requests a minute per account (in memory; one server machine)."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.seen: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        window = self.seen[key]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            return False
        window.append(now)
        return True


def token_of(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def account_of(request: Request) -> dict:
    """The account behind the Bearer token (and a fair share of requests per minute)."""
    token = token_of(request)
    account = request.app.state.store.account_for_token(token) if token else None
    if account is None:
        raise HTTPException(status_code=401, detail="Sign in to Bridge again.")
    if not request.app.state.limiter.allow(account["id"]):
        raise HTTPException(status_code=429, detail="Too many requests. Slow down a little.")
    return account


Account = Annotated[dict, Depends(account_of)]


def create_app(settings: CloudSettings | None = None, *, http: httpx.AsyncClient | None = None):
    settings = settings or CloudSettings()
    store = Store(settings.database_path)
    limiter = Limiter(settings.requests_per_minute)

    @asynccontextmanager
    async def lifespan(app):
        client = http or httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10))
        app.state.http = client
        app.state.lemon = Lemon(settings, store, client)
        try:
            yield
        finally:
            if http is None:
                await client.aclose()

    app = FastAPI(title="Bridge Cloud", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.exception_handler(HTTPException)
    async def both_shapes(request: Request, exc: HTTPException):
        """FastAPI's {"detail"} for the app, OpenAI's {"error"} for the OpenAI library."""
        return JSONResponse(
            {"detail": exc.detail, "error": {"message": exc.detail, "type": "bridge_cloud"}},
            status_code=exc.status_code,
        )

    app.state.store, app.state.settings, app.state.limiter = store, settings, limiter

    # Who's asking ---------------------------------------------------------------------------

    def client_ip(request: Request) -> str:
        return request.headers.get("fly-client-ip") or (
            request.client.host if request.client else ""
        )

    # Credit -----------------------------------------------------------------------------------

    def charge(account_id: str, amount: int, detail: str) -> None:
        if amount > 0:
            store.add(account_id, -amount, "usage", "use:" + uuid.uuid4().hex, detail)

    def settle_voice(account_id: str) -> None:
        """Conversations nobody ended: charged at the longest a conversation can last."""
        cutoff = time.time() - settings.voice_max_minutes * 60
        for voice in store.open_voice(account_id, cutoff):
            if store.end_voice(voice["id"], account_id):
                rate = settings.voice_minute.get(
                    voice["model"], max(settings.voice_minute.values())
                )
                charge(account_id, micros(rate * settings.voice_max_minutes), voice["model"])

    def overview(account: dict) -> dict:
        settle_voice(account["id"])
        balance = store.balance(account["id"])
        return {
            "account": account["id"],
            "plan": account["plan"],
            "status": account["status"],
            "renews_at": account["renews_at"],
            "ends_at": account["ends_at"],
            "balance": round(balance / MICRO, 6),
            "spent_30_days": round(
                store.spent_since(account["id"], time.time() - 30 * 86400) / MICRO, 4
            ),
            "devices": store.devices(account["id"]),
            "history": store.history(account["id"]),
            "plans": [
                {
                    "key": p.key,
                    "name": p.name,
                    "price": p.price,
                    "credit": p.credit,
                    "monthly": p.monthly,
                    "for_sale": bool(
                        p.variant_id and settings.lemonsqueezy_api_key.get_secret_value()
                    ),
                }
                for p in settings.plans()
            ],
            "voice_minute": settings.voice_minute,
        }

    # The app's account ------------------------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/v1/devices")
    async def new_device(payload: NewDevice, request: Request):
        ip = client_ip(request)
        if store.trials_from(ip, time.time() - 86400) >= settings.trials_per_ip_per_day:
            raise HTTPException(status_code=429, detail="Too many new accounts from here today.")
        store.note_trial(ip)
        account, token = store.new_account(payload.name)
        store.add(
            account["id"],
            micros(settings.trial_credit),
            "trial",
            f"trial:{account['id']}",
            "Free trial",
        )
        return {"token": token, **overview(account)}

    @app.get("/v1/account")
    async def get_account(account: Account):
        return overview(account)

    @app.post("/v1/checkout")
    async def checkout(payload: Checkout, request: Request, account: Account):
        try:
            return {"url": await request.app.state.lemon.checkout(account, payload.plan)}
        except LemonError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.post("/v1/portal")
    async def portal(request: Request, account: Account):
        try:
            return {"url": await request.app.state.lemon.portal(account)}
        except LemonError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.post("/v1/link")
    async def link(payload: Link, request: Request, account: Account):
        target = store.account_for_license(payload.license_key)
        if target is None:
            raise HTTPException(status_code=404, detail="That license key isn't known. Check it.")
        store.move_device(token_of(request), target["id"])
        return overview(target)

    @app.post("/v1/sign-out")
    async def sign_out(request: Request, account: Account):
        store.sign_out(token_of(request))
        return {"signed_out": True}

    @app.get("/thanks", response_class=HTMLResponse)
    async def thanks():
        return (
            "<!doctype html><meta charset=utf-8><title>Thank you</title>"
            "<body style='font:16px -apple-system,sans-serif;text-align:center;padding:80px'>"
            "<h1>Thank you!</h1><p>Your Bridge credit is ready in a moment. "
            "You can close this page and go back to Bridge.</p>"
        )

    # Lemon Squeezy ----------------------------------------------------------------------------

    @app.post("/v1/webhooks/lemonsqueezy")
    async def webhook(request: Request):
        body = await request.body()
        secret = settings.lemonsqueezy_webhook_secret.get_secret_value()
        if not signed(body, request.headers.get("x-signature", ""), secret):
            raise HTTPException(status_code=401, detail="Bad signature.")
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(status_code=400, detail="Not JSON.") from None
        return {"result": request.app.state.lemon.handle(payload)}

    # OpenAI, paid from credit -----------------------------------------------------------------

    def upstream_headers(content_type: str = "application/json") -> dict:
        key = settings.openai_api_key.get_secret_value()
        if not key:
            raise HTTPException(status_code=503, detail="The AI service isn't set up yet.")
        return {"Authorization": f"Bearer {key}", "Content-Type": content_type}

    def ready(account: dict, minimum: int = 1) -> JSONResponse | None:
        settle_voice(account["id"])
        if store.balance(account["id"]) < minimum:
            return openai_error(402, OUT_OF_CREDIT, "out_of_credit")
        return None

    @app.get("/v1/openai/models")
    async def models(account: Account):
        return {"object": "list", "data": [{"id": m, "object": "model"} for m in PRICES]}

    @app.post("/v1/openai/{path:path}")
    async def openai(path: str, request: Request, account: Account):
        body = await request.body()
        if len(body) > settings.max_body_bytes:
            return openai_error(413, "That request is too large.")
        if path in JSON_PATHS:
            return await text(path, body, request, account)
        if path == "audio/speech":
            return await speech(body, request, account)
        if path == "audio/transcriptions":
            return await transcribe(body, request, account)
        if path == "realtime/client_secrets":
            return await voice(body, request, account)
        return openai_error(404, "Bridge doesn't offer that part of OpenAI.")

    async def text(path: str, body: bytes, request: Request, account: dict):
        try:
            data = json.loads(body)
        except ValueError:
            return openai_error(400, "Not JSON.")
        model = str(data.get("model") or "")
        if not allowed(model):
            return openai_error(400, f"Bridge doesn't offer the model {model[:40]!r}.")
        if (refused := ready(account)) is not None:
            return refused
        stream = bool(data.get("stream"))
        if stream and path == "chat/completions":
            data["stream_options"] = {"include_usage": True}
        payload = json.dumps(data).encode()
        url = f"{settings.openai_base}/{path}"
        client: httpx.AsyncClient = request.app.state.http
        cap = data.get("max_output_tokens") or data.get("max_completion_tokens")
        if not stream:
            try:
                answer = await client.post(url, content=payload, headers=upstream_headers())
            except httpx.HTTPError:
                return openai_error(502, "Couldn't reach OpenAI. Try again.")
            if answer.status_code < 400:
                try:
                    usage = answer.json().get("usage")
                except ValueError:
                    usage = None
                cost = usage_micros(model, usage)
                charge(
                    account["id"],
                    cost if cost is not None else guess_micros(model, len(payload), cap),
                    model,
                )
            return Response(
                answer.content,
                status_code=answer.status_code,
                media_type=answer.headers.get("content-type", "application/json"),
            )

        upstream = client.build_request("POST", url, content=payload, headers=upstream_headers())
        try:
            answer = await client.send(upstream, stream=True)
        except httpx.HTTPError:
            return openai_error(502, "Couldn't reach OpenAI. Try again.")
        if answer.status_code >= 400:
            content = await answer.aread()
            await answer.aclose()
            return Response(content, status_code=answer.status_code, media_type="application/json")

        async def relay():
            usage = None
            try:
                async for line in answer.aiter_lines():
                    if line.startswith("data: ") and '"usage"' in line:
                        try:
                            event = json.loads(line[6:])
                        except ValueError:
                            event = {}
                        found = (event.get("response") or {}).get("usage") or event.get("usage")
                        if found:
                            usage = found
                    yield line + "\n"
            finally:
                await answer.aclose()
                cost = usage_micros(model, usage)
                charge(
                    account["id"],
                    cost if cost is not None else guess_micros(model, len(payload), cap),
                    model,
                )

        return StreamingResponse(relay(), media_type="text/event-stream")

    async def speech(body: bytes, request: Request, account: dict):
        try:
            data = json.loads(body)
        except ValueError:
            return openai_error(400, "Not JSON.")
        if (refused := ready(account)) is not None:
            return refused
        try:
            answer = await request.app.state.http.post(
                f"{settings.openai_base}/audio/speech", content=body, headers=upstream_headers()
            )
        except httpx.HTTPError:
            return openai_error(502, "Couldn't reach OpenAI. Try again.")
        if answer.status_code < 400:
            chars = len(str(data.get("input") or ""))
            charge(
                account["id"],
                micros(math.ceil(chars / 1000) * settings.speech_per_1k_chars),
                "speech",
            )
        return Response(
            answer.content,
            status_code=answer.status_code,
            media_type=answer.headers.get("content-type", "audio/mpeg"),
        )

    async def transcribe(body: bytes, request: Request, account: dict):
        if (refused := ready(account)) is not None:
            return refused
        kind = request.headers.get("content-type", "")
        if not kind.startswith("multipart/form-data"):
            return openai_error(400, "Send the audio as a form.")
        try:
            answer = await request.app.state.http.post(
                f"{settings.openai_base}/audio/transcriptions",
                content=body,
                headers=upstream_headers(kind),
            )
        except httpx.HTTPError:
            return openai_error(502, "Couldn't reach OpenAI. Try again.")
        if answer.status_code < 400:
            seconds = None
            try:
                usage = answer.json().get("usage") or {}
                seconds = usage.get("seconds") if usage.get("type") == "duration" else None
            except ValueError:
                pass
            minutes = (seconds / 60) if seconds else max(len(body) / 1_000_000, 0.1)  # ~1 MB/min.
            charge(
                account["id"],
                micros(math.ceil(minutes) * settings.transcribe_per_minute),
                "transcribe",
            )
        return Response(
            answer.content,
            status_code=answer.status_code,
            media_type=answer.headers.get("content-type", "application/json"),
        )

    async def voice(body: bytes, request: Request, account: dict):
        try:
            data = json.loads(body)
        except ValueError:
            return openai_error(400, "Not JSON.")
        model = str(((data.get("session") or {}).get("model")) or "")
        rate = settings.voice_minute.get(model)
        if rate is None:
            return openai_error(400, f"Bridge doesn't offer the voice model {model[:40]!r}.")
        if (refused := ready(account, micros(rate * 2))) is not None:
            return refused
        if store.live_voice(account["id"], time.time() - settings.voice_max_minutes * 60) >= 2:
            return openai_error(429, "Two conversations are already open. End one first.")
        try:
            answer = await request.app.state.http.post(
                f"{settings.openai_base}/realtime/client_secrets",
                content=body,
                headers=upstream_headers(),
            )
        except httpx.HTTPError:
            return openai_error(502, "Couldn't reach OpenAI. Try again.")
        if answer.status_code >= 400:
            return Response(
                answer.content, status_code=answer.status_code, media_type="application/json"
            )
        session = answer.json()
        session["bridge_voice_id"] = store.start_voice(account["id"], model)
        return session

    @app.post("/v1/voice/end")
    async def voice_end(payload: VoiceEnd, account: Account):
        voice = store.end_voice(payload.voice_id, account["id"])
        if voice is None:
            return {"charged": 0}
        minutes = min(
            math.ceil((voice["ended"] - voice["started"]) / 60), settings.voice_max_minutes
        )
        amount = micros(settings.voice_minute.get(voice["model"], 0.3) * max(minutes, 1))
        charge(account["id"], amount, voice["model"])
        return {"charged": amount / MICRO, "minutes": max(minutes, 1)}

    return app
