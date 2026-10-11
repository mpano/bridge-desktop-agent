"""Routes for the weekly report: read it, write or rewrite it, edit it, send it.

Sending posts exactly the text in the request (what you see on screen) to the one Slack
channel or person, or the email addresses, you typed; nothing is sent any other way.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

WEEK = r"^\d{4}-W\d{2}$"


class Write(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fresh: bool = False


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    week: str = Field(pattern=WEEK)
    text: str = Field(min_length=1, max_length=8000)


class Send(Edit):
    slack: str = Field(default="", max_length=80, pattern=r"^[^\x00-\x1f\x7f]*$")
    email: list[str] = Field(default_factory=list, max_length=10)


def install(app: FastAPI, authorize) -> None:
    def reports(request: Request):
        found = getattr(request.app.state.agent, "reports", None)
        if found is None:
            raise HTTPException(404, "The weekly report isn't available.")
        return found

    @app.get("/api/v1/report", dependencies=[Depends(authorize)])
    async def report(request: Request):
        return reports(request).view()

    @app.post("/api/v1/report/write", dependencies=[Depends(authorize)])
    async def write(payload: Write, request: Request):
        try:
            return await reports(request).write(fresh=payload.fresh)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/v1/report/edit", dependencies=[Depends(authorize)])
    async def edit(payload: Edit, request: Request):
        try:
            return reports(request).edit(payload.week, payload.text)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/v1/report/send", dependencies=[Depends(authorize)])
    async def send(payload: Send, request: Request):
        try:
            return await reports(request).send(
                payload.week, payload.text, slack=payload.slack, email=payload.email
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:
            raise HTTPException(502, str(exc) or "It wasn't sent. Try again.") from None
