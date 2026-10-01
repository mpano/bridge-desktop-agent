"""Routes for the Automations screen: routines, alerts, scheduled requests and follow-ups.

Routines (Plan my day, the evening wrap-up) own their schedules; they change only through
their switches, so the list can't get out of step with the settings.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request

from app.api.schemas import ForgetRequest


def install(app: FastAPI, authorize) -> None:
    @app.get("/api/v1/automations", dependencies=[Depends(authorize)])
    async def automations(request: Request):
        agent = request.app.state.agent
        settings = agent.proactive_store.settings()
        routine_ids = {settings.get("morning_schedule_id"), settings.get("evening_schedule_id")}
        listed = await agent.proactive_controller.list(None)
        return {
            "settings": settings,
            "watches": listed["watches"],
            "schedules": [
                {
                    "id": item.id,
                    "message": item.message,
                    "when": item.describe(),
                    "next_run": item.next_run,
                    "last_status": item.last_status,
                }
                for item in agent.schedule_store.list()
                if item.id not in routine_ids and item.next_run
            ],
            "followups": [
                {"id": f.id, "name": f.name, "about": f.about, "due": f.due, "status": f.status}
                for f in agent.proactive_store.followups()
                if f.status in {"waiting", "overdue", "replied"}
            ][-20:],
        }

    @app.post("/api/v1/schedules/delete", dependencies=[Depends(authorize)])
    async def delete_schedule(payload: ForgetRequest, request: Request):
        agent = request.app.state.agent
        settings = agent.proactive_store.settings()
        if payload.id in {settings.get("morning_schedule_id"), settings.get("evening_schedule_id")}:
            raise HTTPException(409, "Turn this routine off with its switch instead.")
        if not agent.schedule_store.delete(payload.id):
            raise HTTPException(404, "That scheduled request was already removed.")
        return {"removed": payload.id}
