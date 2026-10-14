"""Routes for talking with Bridge: start a conversation, run its tools, approve, end."""

from __future__ import annotations

from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.assistant.talk import TalkError

SESSION = Field(min_length=10, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Start(Body):
    name: str = Field(default="", max_length=80)


class ToolCall(Body):
    session_id: str = SESSION
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z_][a-z0-9_]*$")
    arguments: dict = Field(default_factory=dict)


class Approve(Body):
    session_id: str = SESSION
    approve: bool


class Said(Body):
    session_id: str = SESSION
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=8000)


class Tokens(Body):
    input: int = Field(default=0, ge=0, le=50_000_000)
    cached: int = Field(default=0, ge=0, le=50_000_000)
    output: int = Field(default=0, ge=0, le=50_000_000)


class End(Body):
    session_id: str = SESSION
    seconds: float = Field(ge=0, le=7200)
    tokens: Tokens | None = None


def install(app: FastAPI, authorize, from_phone) -> None:
    def talk(request: Request):
        found = getattr(request.app.state.agent, "talk", None)
        if found is None:
            raise HTTPException(404, "Talking isn't available.")
        return found

    @app.get("/api/v1/talk", dependencies=[Depends(authorize)])
    async def status(request: Request):
        found = talk(request)
        return {
            "enabled": found.enabled,
            "model": found.settings.talk_model,
            "voice": found.settings.talk_voice,
            "usage": found.usage(),
        }

    @app.get("/api/v1/talk/voices", dependencies=[Depends(authorize)])
    async def voices(request: Request):
        return {"voices": talk(request).voices()}

    @app.get("/api/v1/talk/sample/{voice}", dependencies=[Depends(authorize)])
    async def sample(voice: str, request: Request):
        try:
            path = await talk(request).sample(voice)
        except TalkError as exc:
            raise HTTPException(409, str(exc)) from None
        return FileResponse(path, media_type="audio/mpeg")

    @app.post("/api/v1/talk/start", dependencies=[Depends(authorize)])
    async def start(payload: Start, request: Request):
        try:
            return await talk(request).start(from_phone=from_phone(request), name=payload.name)
        except TalkError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/v1/talk/tool", dependencies=[Depends(authorize)])
    async def tool(payload: ToolCall, request: Request):
        try:
            return await talk(request).tool(payload.session_id, payload.name, payload.arguments)
        except TalkError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/v1/talk/approve", dependencies=[Depends(authorize)])
    async def approve(payload: Approve, request: Request):
        try:
            return await talk(request).approve(payload.session_id, payload.approve)
        except TalkError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/v1/talk/said", dependencies=[Depends(authorize)])
    async def said(payload: Said, request: Request):
        try:
            await talk(request).log(payload.session_id, payload.role, payload.text)
        except TalkError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"saved": True}

    @app.post("/api/v1/talk/end", dependencies=[Depends(authorize)])
    async def end(payload: End, request: Request):
        tokens = payload.tokens.model_dump() if payload.tokens else None
        found = talk(request)
        usage = found.end(payload.session_id, payload.seconds, tokens)
        cloud = getattr(request.app.state.agent, "cloud", None)
        while found.ended_voice:
            voice_id = found.ended_voice.pop()
            if cloud is not None:
                await cloud.voice_ended(voice_id)  # A Bridge plan charges by the minute.
        return {"usage": usage}
