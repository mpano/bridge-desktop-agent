"""Routes for the brief on Today: the morning brief or the evening wrap-up, and moving
slipped promises to the next working day with one tap."""

from __future__ import annotations

from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


class Move(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=50)


def install(app: FastAPI, authorize) -> None:
    def briefs(request: Request):
        found = getattr(request.app.state.agent, "briefs", None)
        if found is None:
            raise HTTPException(404, "Briefs aren't available.")
        return found

    @app.get("/api/v1/brief", dependencies=[Depends(authorize)])
    async def brief(
        request: Request,
        kind: Literal["auto", "morning", "evening"] = "auto",
        refresh: bool = False,
    ):
        found = briefs(request)
        if kind == "auto":
            return await found.current(refresh=refresh)
        if kind == "evening":
            return await found.evening()
        return await found.morning(refresh=refresh)

    @app.post("/api/v1/brief/move", dependencies=[Depends(authorize)])
    async def move(payload: Move, request: Request):
        return briefs(request).move_to_next_day(payload.ids)
