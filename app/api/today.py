"""Routes for the Today screen: your day, the inbox at a glance, approvals and focus.

Buttons on Today are the owner's own clicks in the signed-in window, so "add the plan to
my calendar" or "start focus" run directly here, the same way approving a card does.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

INBOX_FRESH_SECONDS = 30 * 60
log = logging.getLogger(__name__)


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: Literal["today", "tomorrow"] = "today"


class PlanApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: int = Field(ge=1)


class FocusStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minutes: int = Field(default=60, ge=10, le=240)
    task: str = Field(default="", max_length=80)


def inbox_summary(*sources) -> dict | None:
    """Email and Slack together: what needs you, from each source's last sort."""
    sorted_ = [getattr(source, "last", None) for source in sources if source is not None]
    sorted_ = [item for item in sorted_ if item]
    if not sorted_:
        return None
    counts = {"urgent": 0, "reply": 0, "fyi": 0, "newsletter": 0}
    top = []
    for _at, data in sorted_:
        for key, items in data["groups"].items():
            counts[key] = counts.get(key, 0) + len(items)
        for key in ("urgent", "reply"):
            for item in data["groups"].get(key, []):
                top.append(
                    {
                        "group": key,
                        "source": item.get("source", "gmail"),
                        "from": item["from"].split("<")[0].strip().strip('"') or item["from"],
                        "subject": item["subject"],
                        "summary": item["summary"],
                    }
                )
    at = min(at for at, _ in sorted_)
    return {
        "at": max(at for at, _ in sorted_),
        "fresh": time.time() - at < INBOX_FRESH_SECONDS,
        "counts": counts,
        "top": sorted(top, key=lambda item: item["group"] != "urgent")[:4],
    }


async def sort_everything(agent) -> None:
    """Sort email and Slack at the same time; one failing doesn't stop the other."""
    jobs = [
        source.triage()
        for source in (getattr(agent, "inbox", None), getattr(agent, "slack_inbox", None))
        if source is not None
    ]
    results = await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), timeout=150)
    errors = [result for result in results if isinstance(result, Exception)]
    for error in errors:
        log.warning(
            "Inbox sorting failed for one source: %s",
            type(error).__name__ + ": " + str(error)[:200],
        )
    if errors and len(errors) == len(results):
        raise errors[0]


def install(app: FastAPI, authorize) -> None:
    def agent(request: Request):
        return request.app.state.agent

    @app.get("/api/v1/today", dependencies=[Depends(authorize)])
    async def today(request: Request):
        bridge = agent(request)
        now = datetime.now().astimezone()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        events, calendar_error = [], None
        calendar = getattr(bridge, "calendar", None)
        if calendar is not None:
            window = SimpleNamespace(
                start=start.isoformat(), end=(start + timedelta(days=1)).isoformat()
            )
            try:
                found = await calendar.events(window)
                events = [
                    {
                        key: event.get(key)
                        for key in ("title", "start", "end", "all_day", "location")
                    }
                    for event in found["events"]
                ]
            except Exception:
                calendar_error = "Allow Calendar access for Bridge to see your day."
        store = getattr(bridge, "proactive_store", None)
        plan = store.latest_plan(start.date().isoformat()) if store is not None else None
        settings = store.settings() if store is not None else {}
        focus = getattr(bridge, "focus", None)
        return {
            "now": now.isoformat(),
            "events": events,
            "calendar_error": calendar_error,
            "plan": {"id": plan[0], "blocks": plan[1]} if plan else None,
            "followups": [
                {"id": f.id, "name": f.name, "about": f.about, "due": f.due, "status": f.status}
                for f in (store.followups() if store is not None else [])
                if f.status in {"waiting", "overdue"}
            ],
            "focus": focus.status() if focus is not None else {"active": False},
            "approvals": bridge.confirmations.count(),
            "inbox": inbox_summary(
                getattr(bridge, "inbox", None), getattr(bridge, "slack_inbox", None)
            ),
            "gmail": getattr(bridge, "inbox", None) is not None
            or getattr(bridge, "slack_inbox", None) is not None,
            "work": {"start": settings.get("work_start"), "end": settings.get("work_end")},
        }

    @app.post("/api/v1/today/plan", dependencies=[Depends(authorize)])
    async def make_plan(payload: PlanRequest, request: Request):
        planner = getattr(agent(request), "day_planner", None)
        if planner is None:
            raise HTTPException(503, "Day planning isn't available.")
        try:
            return await planner.plan(payload.day)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/v1/today/plan/apply", dependencies=[Depends(authorize)])
    async def apply_plan(payload: PlanApply, request: Request):
        bridge = agent(request)
        saved = bridge.proactive_store.plan(payload.plan_id)
        if saved is None or not saved[1]:
            raise HTTPException(404, "That plan has expired. Make a new one.")
        try:
            return await bridge.day_planner.apply(saved[1])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/v1/inbox/summary", dependencies=[Depends(authorize)])
    async def summary(request: Request):
        bridge = agent(request)
        inbox, slack = getattr(bridge, "inbox", None), getattr(bridge, "slack_inbox", None)
        return {
            "gmail": inbox is not None or slack is not None,
            "summary": inbox_summary(inbox, slack),
        }

    @app.post("/api/v1/inbox/triage", dependencies=[Depends(authorize)])
    async def triage(request: Request):
        bridge = agent(request)
        inbox, slack = getattr(bridge, "inbox", None), getattr(bridge, "slack_inbox", None)
        if inbox is None and slack is None:
            raise HTTPException(409, "Connect Gmail or Slack in Connections first.")
        try:
            await sort_everything(bridge)
        except TimeoutError:
            raise HTTPException(504, "That took too long. Try again.") from None
        except Exception as exc:
            raise HTTPException(502, str(exc) or "Couldn't read your inbox.") from None
        return {"gmail": True, "summary": inbox_summary(inbox, slack)}

    @app.get("/api/v1/screen/peek", dependencies=[Depends(authorize)])
    async def peek(request: Request):
        screen = getattr(agent(request), "screen", None)
        if screen is None:
            return {"window": None}
        try:
            return {"window": await asyncio.wait_for(screen.peek(), timeout=3)}
        except Exception:
            return {"window": None}

    @app.get("/api/v1/approvals", dependencies=[Depends(authorize)])
    async def approvals(request: Request):
        return {"approvals": agent(request).pending_approvals()}

    @app.post("/api/v1/focus/start", dependencies=[Depends(authorize)])
    async def start_focus(payload: FocusStart, request: Request):
        focus = getattr(agent(request), "focus", None)
        if focus is None:
            raise HTTPException(503, "Focus mode isn't available.")
        try:
            return await focus.start(payload.minutes, payload.task)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
