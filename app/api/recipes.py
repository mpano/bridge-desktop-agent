"""Routes for recipes ("when X, do Y"): plan one from words, create, change, and approve runs.

A plan is shown before it's saved, with exactly what each action will do. Approving a run
runs the stored actions as they were shown; the page can't change them.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


class Words(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=5, max_length=500)


class Create(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: dict[str, Any]


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=1)
    mode: Literal["ask", "auto"] | None = None
    enabled: bool | None = None


class Ref(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=1)


class Decide(Ref):
    approve: bool


def install(app: FastAPI, authorize) -> None:
    def recipes(request: Request):
        found = getattr(request.app.state.agent, "recipes", None)
        if found is None:
            raise HTTPException(404, "Recipes aren't available.")
        return found

    @app.get("/api/v1/recipes", dependencies=[Depends(authorize)])
    async def listing(request: Request):
        return recipes(request).overview()

    @app.post("/api/v1/recipes/plan", dependencies=[Depends(authorize)])
    async def plan(payload: Words, request: Request):
        found = recipes(request)
        try:
            return found.preview(found.check(await found.understand(payload.text)))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/v1/recipes", dependencies=[Depends(authorize)])
    async def create(payload: Create, request: Request):
        try:
            return await recipes(request).create(payload.plan)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/v1/recipes/update", dependencies=[Depends(authorize)])
    async def update(payload: Change, request: Request):
        try:
            return recipes(request).update(payload.id, mode=payload.mode, enabled=payload.enabled)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/v1/recipes/delete", dependencies=[Depends(authorize)])
    async def delete(payload: Ref, request: Request):
        try:
            return recipes(request).delete(payload.id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/v1/recipes/runs/decide", dependencies=[Depends(authorize)])
    async def decide(payload: Decide, request: Request):
        try:
            return await recipes(request).decide(payload.id, payload.approve)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
