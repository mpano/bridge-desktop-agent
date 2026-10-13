"""Routes for reminders for a moment (when you open an app, a meeting starts, you're back)."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


class Words(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=5, max_length=400)


class Ref(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=1)


def install(app: FastAPI, authorize) -> None:
    def moments(request: Request):
        found = getattr(request.app.state.agent, "moments", None)
        if found is None:
            raise HTTPException(404, "Reminders for a moment aren't available.")
        return found

    @app.get("/api/v1/moments", dependencies=[Depends(authorize)])
    async def listing(request: Request):
        found = moments(request)
        return {"moments": found.overview(), "available": found.sense is not None}

    @app.post("/api/v1/moments", dependencies=[Depends(authorize)])
    async def add(payload: Words, request: Request):
        try:
            return await moments(request).add(payload.text)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/v1/moments/remove", dependencies=[Depends(authorize)])
    async def remove(payload: Ref, request: Request):
        if not moments(request).store.remove(payload.id):
            raise HTTPException(404, "Already removed.")
        return {"removed": payload.id}
