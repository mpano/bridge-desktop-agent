"""Routes for Show me how: start a guide, follow it, Next / Do this step / Stop, read a
step aloud, and save it to Notes. Only from this Mac (the phone can't reach these)."""

from __future__ import annotations

from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.assistant.operator import OperatorError

GUIDE = Field(min_length=6, max_length=20, pattern=r"^[0-9a-f]+$")


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=5, max_length=500)


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = GUIDE
    what: Literal["next", "do", "rest", "stop", "answer"]
    answer: str = Field(default="", max_length=500)


class Ref(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = GUIDE


class Speak(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=400)


def install(app: FastAPI, authorize) -> None:
    def guides(request: Request):
        found = getattr(request.app.state.agent, "guides", None)
        if found is None:
            raise HTTPException(404, "Show me how isn't available.")
        return found

    @app.get("/api/v1/guide", dependencies=[Depends(authorize)])
    async def current(request: Request):
        found = guides(request)
        return {
            "current": found.current.view() if found.current else None,
            "recent": found.recent(),
            "available": found.hands is not None,
            "enabled": bool(getattr(found.settings, "operator_enabled", False)),
        }

    @app.post("/api/v1/guide/start", dependencies=[Depends(authorize)])
    async def start(payload: Start, request: Request):
        try:
            return guides(request).start(payload.task)
        except OperatorError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/v1/guide/command", dependencies=[Depends(authorize)])
    async def command(payload: Command, request: Request):
        try:
            return guides(request).command(payload.id, payload.what, payload.answer)
        except OperatorError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/v1/guide/save", dependencies=[Depends(authorize)])
    async def save(payload: Ref, request: Request):
        try:
            return await guides(request).save(payload.id)
        except OperatorError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/v1/guide/speak", dependencies=[Depends(authorize)])
    async def speak(payload: Speak, request: Request):
        try:
            audio = await guides(request).speak(payload.text)
        except OperatorError as exc:
            raise HTTPException(409, str(exc)) from None
        return Response(audio, media_type="audio/mpeg")
