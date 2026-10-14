"""Settings › About: which version you have, and getting the next one."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request

from app.desktop.updates import UpdateError


def install(app: FastAPI, authorize) -> None:
    def updates(request: Request):
        found = getattr(request.app.state.agent, "updates", None)
        if found is None:
            raise HTTPException(status_code=404, detail="Updates aren't available here.")
        return found

    @app.get("/api/v1/updates", dependencies=[Depends(authorize)])
    async def overview(request: Request):
        return updates(request).overview()

    @app.post("/api/v1/updates/check", dependencies=[Depends(authorize)])
    async def check(request: Request):
        return await updates(request).check()

    @app.post("/api/v1/updates/download", dependencies=[Depends(authorize)])
    async def download(request: Request):
        try:
            return await updates(request).download()
        except UpdateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.post("/api/v1/updates/install", dependencies=[Depends(authorize)])
    async def install_now(request: Request):
        try:
            return updates(request).install()
        except UpdateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
