"""Routes for Settings › Learning: lessons Bridge proposes from your corrections, the ones in
use, this week's report, and 👍/👎 on answers in Ask."""

from __future__ import annotations

from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Decide(Body):
    id: int = Field(ge=1)
    status: Literal["active", "rejected", "off"]


class Feedback(Body):
    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[0-9a-f]+$")
    good: bool
    note: str = Field(default="", max_length=500)


def install(app: FastAPI, authorize) -> None:
    def learning(request: Request):
        found = getattr(request.app.state.agent, "learning", None)
        if found is None:
            raise HTTPException(404, "Learning isn't available.")
        return found

    @app.get("/api/v1/learning", dependencies=[Depends(authorize)])
    async def overview(request: Request):
        found = learning(request)
        return {
            "enabled": found.enabled,
            "proposed": found.rules("proposed"),
            "active": found.rules("active") + found.rules("off"),
            "week": found.week(),
            "waiting": len(found.cases(unused=True)),
        }

    @app.post("/api/v1/learning/propose", dependencies=[Depends(authorize)])
    async def propose(request: Request):
        made = await learning(request).propose()
        return {"proposed": made}

    @app.post("/api/v1/learning/decide", dependencies=[Depends(authorize)])
    async def decide(payload: Decide, request: Request):
        try:
            learning(request).decide(payload.id, payload.status)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"saved": True}

    @app.post("/api/v1/learning/feedback", dependencies=[Depends(authorize)])
    async def feedback(payload: Feedback, request: Request):
        agent = request.app.state.agent
        said = [m for m in agent.conversation() if m.get("request_id") == payload.request_id]
        asked = next((m["text"] for m in said if m["role"] == "user"), "")
        answer = next((m["text"] for m in said if m["role"] == "assistant"), "")
        if not payload.good:
            learning(request).record(
                "ask",
                "thumbs_down",
                payload.note or asked,
                {
                    "request": asked,
                    "answer": answer[:1500],
                    "what_was_wrong": payload.note,
                },
            )
        return {"thanks": True}

    @app.post("/api/v1/learning/forget", dependencies=[Depends(authorize)])
    async def forget(request: Request):
        learning(request).forget()
        return {"forgotten": True}
