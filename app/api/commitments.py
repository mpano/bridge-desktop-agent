"""Routes for promises on the Today screen: list, check now, done/dismiss/date, follow up.

A follow-up goes where the promise was made (the same email thread or Slack conversation);
the recipient comes from the stored message, never the page, and only the owner's Send
click sends the exact text shown.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


class Ref(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[0-9a-f]{20}$")


class Change(Ref):
    status: Literal["open", "done", "dismissed"] | None = None
    due: date | Literal[""] | None = None  # "" clears the date.


class Draft(Ref):
    name: str = Field(default="", max_length=80)


class Send(Ref):
    body: str = Field(min_length=1, max_length=8000)


def link(item: dict) -> str | None:
    ref = item["ref"]
    if item["source"] == "slack":
        return ref.get("permalink")
    if item["source"] == "email" and ref.get("thread_id"):
        return f"https://mail.google.com/mail/u/0/#all/{quote(ref['thread_id'], safe='')}"
    return None


def view(item: dict) -> dict:
    ref = item["ref"]
    sendable = item["direction"] == "theirs" and (
        (item["source"] == "slack" and ref.get("channel"))
        or (item["source"] == "email" and ref.get("address") and ref.get("reply_message_id"))
    )
    return {
        "id": item["id"],
        "direction": item["direction"],
        "person": item["person"],
        "what": item["what"],
        "due": item["due"],
        "source": item["source"],
        "quote": item["quote"],
        "status": item["status"],
        "closed_by": item["closed_by"],
        "said_at": item["said_at"],
        "followed_up": item["followed_up"],
        "link": link(item),
        "can_follow_up": bool(sendable),
    }


def install(app: FastAPI, authorize) -> None:
    def tracker(request: Request):
        found = getattr(request.app.state.agent, "commitments", None)
        if found is None:
            raise HTTPException(404, "Promises aren't available.")
        return found

    def find(request: Request, commitment_id: str) -> dict:
        item = tracker(request).store.get(commitment_id)
        if item is None:
            raise HTTPException(404, "That promise is gone. Refresh.")
        return item

    @app.get("/api/v1/commitments", dependencies=[Depends(authorize)])
    async def listing(request: Request):
        found = tracker(request)
        open_items = [view(item) for item in found.store.items("open")]
        recently = [
            view(item)
            for item in found.store.items("done", since=time.time() - 2 * 86400)
            if item["closed_by"] == "bridge"
        ]
        return {
            "connected": found.connected,
            "scan": found.status(),
            "today": date.today().isoformat(),
            "mine": [item for item in open_items if item["direction"] == "mine"],
            "theirs": [item for item in open_items if item["direction"] == "theirs"],
            "closed_by_bridge": recently,
        }

    @app.post("/api/v1/commitments/scan", dependencies=[Depends(authorize)])
    async def scan(request: Request):
        found = tracker(request)
        if not found.connected:
            raise HTTPException(409, "Connect Gmail or Slack first.")
        return found.start_scan()

    @app.post("/api/v1/commitments/update", dependencies=[Depends(authorize)])
    async def update(payload: Change, request: Request):
        find(request, payload.id)
        changes = {}
        if payload.status is not None:
            changes["status"] = payload.status
            changes["closed_by"] = None if payload.status == "open" else "you"
        if payload.due is not None:
            changes["due"] = payload.due.isoformat() if payload.due else None
            changes["nudged"] = None
        if not changes:
            raise HTTPException(422, "Nothing to change.")
        tracker(request).store.update(payload.id, **changes)
        return view(find(request, payload.id))

    @app.post("/api/v1/commitments/draft", dependencies=[Depends(authorize)])
    async def draft(payload: Draft, request: Request):
        item = find(request, payload.id)
        if not view(item)["can_follow_up"]:
            raise HTTPException(422, "Follow up on this one where it was said.")
        try:
            body = await tracker(request).draft_follow_up(item, payload.name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception:
            raise HTTPException(502, "Couldn't write a follow-up. Try again.") from None
        return {"body": body, "to": item["person"], "source": item["source"]}

    @app.post("/api/v1/commitments/send", dependencies=[Depends(authorize)])
    async def send(payload: Send, request: Request):
        item = find(request, payload.id)
        if not view(item)["can_follow_up"]:
            raise HTTPException(422, "Follow up on this one where it was said.")
        try:
            return await tracker(request).send_follow_up(item, payload.body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:
            raise HTTPException(502, str(exc) or "It wasn't sent. Try again.") from None
