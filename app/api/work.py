"""Route for the Work card on Today: Jira issues assigned to you, pull requests waiting for
your review, and your own pull requests with their checks."""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, Request

from app.integrations.models import IntegrationError


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
