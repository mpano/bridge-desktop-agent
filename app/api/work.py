"""Route for the Work card on Today: Jira issues assigned to you, pull requests waiting for
your review, and your own pull requests with their checks."""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.integrations.models import IntegrationError


class RepoRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(pattern=r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")


def install(app: FastAPI, authorize) -> None:
    @app.get("/api/v1/work", dependencies=[Depends(authorize)])
    async def work(request: Request, fresh: bool = False):
        agent = request.app.state.agent
        jira, github = getattr(agent, "jira", None), getattr(agent, "github", None)
        result = {"jira": {"connected": False}, "github": {"connected": False}}

        async def safely(job):
            try:
                return await job, None
            except (IntegrationError, ValueError) as exc:
                return None, str(exc)
            except Exception:
                return None, "Couldn't load this right now."

        if jira is not None and await jira.connected():
            issues, error = await safely(jira.mine(fresh=fresh))
            result["jira"] = {"connected": True, "issues": (issues or [])[:8], "error": error}
        if github is not None and await github.connected():
            (reviews, e1), (mine, e2) = await asyncio.gather(
                safely(github.reviews(fresh=fresh)), safely(github.my_prs(fresh=fresh))
            )
            result["github"] = {
                "connected": True,
                "reviews": (reviews or [])[:6],
                "mine": (mine or [])[:6],
                "error": e1 or e2,
            }
        return result

    def watcher(request: Request):
        found = getattr(request.app.state.agent, "repos", None)
        github = getattr(request.app.state.agent, "github", None)
        if found is None or github is None:
            raise HTTPException(404, "GitHub isn't available.")
        return found

    @app.get("/api/v1/repos", dependencies=[Depends(authorize)])
    async def repos(request: Request, fresh: bool = False):
        found = watcher(request)
        if not await found.github.connected():
            return {"connected": False, "repos": []}
        try:
            return {"connected": True, **await found.feed(fresh=fresh)}
        except IntegrationError as exc:
            return {"connected": True, "repos": [], "error": str(exc)}

    @app.post("/api/v1/repos/pull", dependencies=[Depends(authorize)])
    async def pull(payload: RepoRef, request: Request):
        try:
            return await watcher(request).pull(payload.repo)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/v1/repos/seen", dependencies=[Depends(authorize)])
    async def seen(payload: RepoRef, request: Request):
        watcher(request).mark_seen(payload.repo.lower())
        return {"seen": True}
