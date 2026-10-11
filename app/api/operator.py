"""Routes for Bridge using your Mac: what it's doing now, Go ahead / Stop, recent tasks.

Only from this Mac (the phone can't reach these); the panel at the bottom of the screen polls
what's happening and sends your Go ahead or Stop.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.assistant.operator import OperatorError

RUN = Field(min_length=6, max_length=20, pattern=r"^[0-9a-f]+$")


class Decide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = RUN
    ok: bool


class StopRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = RUN


def install(app: FastAPI, authorize) -> None:
    def operator(request: Request):
        found = getattr(request.app.state.agent, "operator", None)
        if found is None:
            raise HTTPException(404, "Using the Mac isn't available.")
        return found

    @app.get("/api/v1/operator", dependencies=[Depends(authorize)])
    async def overview(request: Request):
        found = operator(request)
        settings = found.settings
        return {
            "enabled": found.enabled,
            "available": found.hands is not None,
            "ask": settings.operator_ask,
            "model": settings.operator_model,
            "current": found.current.view() if found.current else None,
            "recent": found.recent(),
        }

    @app.get("/api/v1/operator/current", dependencies=[Depends(authorize)])
    async def current(request: Request):
        found = operator(request)
        return {"current": found.current.view() if found.current else None}

    @app.post("/api/v1/operator/decide", dependencies=[Depends(authorize)])
    async def decide(payload: Decide, request: Request):
        try:
            return operator(request).decide(payload.run_id, payload.ok)
        except OperatorError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/v1/operator/stop", dependencies=[Depends(authorize)])
    async def stop(payload: StopRun, request: Request):
        try:
            return operator(request).stop(payload.run_id)
        except OperatorError as exc:
            raise HTTPException(409, str(exc)) from None
