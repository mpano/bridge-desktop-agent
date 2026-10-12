"""Routes for meeting notes: what's recording, past meetings, start/stop, ask, follow-up.

Starting, pausing and stopping only happen on the Mac (the phone can read and ask). The
follow-up email goes exactly as shown, to the addresses in the request, from your Gmail.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.errors import friendly
from app.assistant.meetings import MeetingError

MEETING = Field(min_length=6, max_length=20, pattern=r"^[0-9a-f]+$")
EMAIL = r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$"


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="", max_length=120)


class Pause(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool


class Ask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=2, max_length=500)
    id: str | None = Field(default=None, max_length=20, pattern=r"^[0-9a-f]+$")


class Ref(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = MEETING


class FollowUp(Ref):
    to: list[str] = Field(min_length=1, max_length=20)
    subject: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")
    body: str = Field(min_length=1, max_length=8000)


def install(app: FastAPI, authorize) -> None:
    def meetings(request: Request):
        found = getattr(request.app.state.agent, "meetings", None)
        if found is None:
            raise HTTPException(404, "Meeting notes aren't available.")
        return found

    async def run(work):
        try:
            return await work
        except MeetingError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/v1/meetings", dependencies=[Depends(authorize)])
    async def overview(request: Request):
        return meetings(request).overview()

    @app.get("/api/v1/meetings/detail", dependencies=[Depends(authorize)])
    async def detail(id: str, request: Request):
        found = meetings(request)
        meeting = found._find(id) if len(id) <= 20 and id.isalnum() else None
        if meeting is None:
            raise HTTPException(404, "That meeting is gone.")
        return found.view(meeting, full=True)

    @app.post("/api/v1/meetings/start", dependencies=[Depends(authorize)])
    async def start(payload: Start, request: Request):
        return await run(meetings(request).start(payload.title))

    @app.post("/api/v1/meetings/stop", dependencies=[Depends(authorize)])
    async def stop(request: Request):
        return await run(meetings(request).stop())

    @app.post("/api/v1/meetings/pause", dependencies=[Depends(authorize)])
    async def pause(payload: Pause, request: Request):
        try:
            return meetings(request).pause(payload.paused)
        except MeetingError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/v1/meetings/ask", dependencies=[Depends(authorize)])
    async def ask(payload: Ask, request: Request):
        try:
            return {"answer": await meetings(request).ask(payload.question, payload.id)}
        except MeetingError as exc:
            raise HTTPException(409, str(exc)) from None
        except Exception:
            raise HTTPException(502, "Couldn't answer right now. Try again.") from None

    @app.post("/api/v1/meetings/followup", dependencies=[Depends(authorize)])
    async def followup(payload: FollowUp, request: Request):
        import re

        if not all(re.fullmatch(EMAIL, address.strip()) for address in payload.to):
            raise HTTPException(422, "Use full email addresses.")
        try:
            return await meetings(request).send_followup(
                payload.id, [a.strip() for a in payload.to], payload.subject, payload.body
            )
        except MeetingError as exc:
            raise HTTPException(409, str(exc)) from None
        except Exception as exc:
            raise HTTPException(502, friendly(exc, "It wasn't sent. Try again.")) from None

    @app.post("/api/v1/meetings/delete", dependencies=[Depends(authorize)])
    async def delete(payload: Ref, request: Request):
        try:
            if not meetings(request).delete(payload.id):
                raise HTTPException(404, "That meeting is already gone.")
        except MeetingError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"removed": payload.id}
