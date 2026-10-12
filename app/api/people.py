"""Routes for People: who you work with, a profile, the brief before your next meeting, and
your notes, merges and "forget". The phone can read; changes happen anywhere you're signed in."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

PERSON = Field(min_length=6, max_length=20, pattern=r"^[0-9a-f]+$")


class Ref(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = PERSON


class Together(Ref):
    fresh: bool = False


class Note(Ref):
    note: str = Field(default="", max_length=2000)


class Rename(Ref):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f]+$")


class Merge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep: str = PERSON
    other: str = PERSON


def install(app: FastAPI, authorize) -> None:
    def people(request: Request):
        found = getattr(request.app.state.agent, "people", None)
        if found is None:
            raise HTTPException(404, "People isn't available.")
        return found

    def run(work):
        try:
            return work()
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.get("/api/v1/people", dependencies=[Depends(authorize)])
    async def listing(request: Request, q: str = ""):
        found = people(request)
        await found.collect()
        return {"people": [found.view(p) for p in found.find(q[:80])]}

    @app.get("/api/v1/people/profile", dependencies=[Depends(authorize)])
    async def profile(request: Request, id: str):
        try:
            return await people(request).profile(id[:20])
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/v1/people/together", dependencies=[Depends(authorize)])
    async def together(payload: Together, request: Request):
        try:
            return await people(request).together(payload.id, fresh=payload.fresh)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        except Exception:
            raise HTTPException(502, "Couldn't write the summary right now.") from None

    @app.get("/api/v1/people/brief", dependencies=[Depends(authorize)])
    async def brief(request: Request):
        try:
            return {"brief": await people(request).next_brief()}
        except Exception:
            return {"brief": None}

    @app.post("/api/v1/people/refresh", dependencies=[Depends(authorize)])
    async def refresh(request: Request):
        return {"changed": await people(request).collect(force=True)}

    @app.post("/api/v1/people/note", dependencies=[Depends(authorize)])
    async def note(payload: Note, request: Request):
        return run(lambda: people(request).note(payload.id, payload.note))

    @app.post("/api/v1/people/rename", dependencies=[Depends(authorize)])
    async def rename(payload: Rename, request: Request):
        return run(lambda: people(request).rename(payload.id, payload.name))

    @app.post("/api/v1/people/merge", dependencies=[Depends(authorize)])
    async def merge(payload: Merge, request: Request):
        return run(lambda: people(request).merge(payload.keep, payload.other))

    @app.post("/api/v1/people/forget", dependencies=[Depends(authorize)])
    async def forget(payload: Ref, request: Request):
        return run(lambda: people(request).forget(payload.id))
