"""Routes for "What's this?" (⌃⌥W) and Bridge on your desk. Only from this Mac."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.assistant.explain import ExplainError
from app.assistant.operator import OperatorError

SPOT = Field(min_length=6, max_length=20, pattern=r"^[0-9a-f]+$")


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=-20000, le=20000)
    y: float = Field(ge=-20000, le=20000)


class Spot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = SPOT


class Question(Spot):
    question: str = Field(min_length=2, max_length=500)


def install(app: FastAPI, authorize) -> None:
    def explainer(request: Request):
        found = getattr(request.app.state.agent, "explainer", None)
        if found is None:
            raise HTTPException(404, "“What's this?” isn't available.")
        return found

    @app.post("/api/v1/explain/look", dependencies=[Depends(authorize)])
    async def look(payload: Point, request: Request):
        try:
            return await explainer(request).look(payload.x, payload.y)
        except ExplainError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/v1/explain", dependencies=[Depends(authorize)])
    async def explain(payload: Spot, request: Request):
        try:
            return await explainer(request).explain(payload.id)
        except ExplainError as exc:
            raise HTTPException(409, str(exc)) from None
        except Exception:
            raise HTTPException(502, "Couldn't explain it right now. Try again.") from None

    @app.post("/api/v1/explain/ask", dependencies=[Depends(authorize)])
    async def ask(payload: Question, request: Request):
        try:
            return {"answer": await explainer(request).follow_up(payload.id, payload.question)}
        except ExplainError as exc:
            raise HTTPException(409, str(exc)) from None
        except Exception:
            raise HTTPException(502, "Couldn't answer right now. Try again.") from None

    @app.post("/api/v1/explain/show-me", dependencies=[Depends(authorize)])
    async def show_me(payload: Spot, request: Request):
        agent = request.app.state.agent
        try:
            task = explainer(request).how_to(payload.id)
            return agent.guides.start(task)
        except (ExplainError, OperatorError) as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/v1/buddy", dependencies=[Depends(authorize)])
    async def buddy(request: Request):
        found = getattr(request.app.state.agent, "buddy", None)
        if found is None:
            raise HTTPException(404, "Bridge on your desk isn't available.")
        return await found.state()
