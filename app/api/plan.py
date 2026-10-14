"""Settings › Plan: a Bridge plan instead of your own OpenAI key (Mac only, not the phone)."""

from __future__ import annotations

from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app.integrations.cloud import CloudError


class Buy(BaseModel):
    plan: Literal["plus", "topup_small", "topup_large"]


class LinkKey(BaseModel):
    license_key: str = Field(min_length=8, max_length=120)


def install(app: FastAPI, authorize, settings) -> None:
    def cloud(request: Request):
        found = getattr(request.app.state.agent, "cloud", None)
        if found is None:
            raise HTTPException(status_code=404, detail="Plans aren't available here.")
        return found

    async def run(work):
        try:
            return await work
        except CloudError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    def state(account: dict | None, error: str = "") -> dict:
        return {
            "provider": settings.ai_provider,
            "own_key": bool(settings.openai_api_key.get_secret_value()),
            "account": account,
            "error": error,
        }

    @app.get("/api/v1/plan", dependencies=[Depends(authorize)])
    async def overview(request: Request):
        try:
            return state(await cloud(request).overview())
        except CloudError as exc:
            return state(None, str(exc))

    @app.post("/api/v1/plan/start", dependencies=[Depends(authorize)])
    async def start(request: Request):
        return state(await run(cloud(request).start()))

    @app.post("/api/v1/plan/own-key", dependencies=[Depends(authorize)])
    async def own_key(request: Request):
        if not settings.openai_api_key.get_secret_value():
            raise HTTPException(status_code=409, detail="Add your OpenAI key first (Privacy).")
        await cloud(request).use_own_key()
        return await overview(request)

    @app.post("/api/v1/plan/checkout", dependencies=[Depends(authorize)])
    async def checkout(payload: Buy, request: Request):
        return {"url": await run(cloud(request).checkout(payload.plan))}

    @app.post("/api/v1/plan/portal", dependencies=[Depends(authorize)])
    async def portal(request: Request):
        return {"url": await run(cloud(request).portal())}

    @app.post("/api/v1/plan/link", dependencies=[Depends(authorize)])
    async def link(payload: LinkKey, request: Request):
        return state(await run(cloud(request).link(payload.license_key)))

    @app.post("/api/v1/plan/sign-out", dependencies=[Depends(authorize)])
    async def sign_out(request: Request):
        await cloud(request).sign_out()
        return state(None)
