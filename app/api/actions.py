"""Routes for Activity: what Bridge did for you, and Undo where it's possible."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


class Ref(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=6, max_length=20, pattern=r"^[0-9a-f]+$")


def install(app: FastAPI, authorize) -> None:
    def actions(request: Request):
        found = getattr(request.app.state.agent, "actions", None)
        if found is None:
            raise HTTPException(404, "Activity isn't available.")
        return found

    @app.get("/api/v1/actions", dependencies=[Depends(authorize)])
    async def listing(request: Request):
        return {"actions": actions(request).recent()}

    @app.post("/api/v1/actions/undo", dependencies=[Depends(authorize)])
    async def undo(payload: Ref, request: Request):
        try:
            return await actions(request).undo(payload.id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
