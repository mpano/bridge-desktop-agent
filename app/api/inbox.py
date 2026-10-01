"""Routes for the Inbox screen: the sorted inbox, one email in full, a draft reply, send.

Send is the owner's own click on the exact text shown in the signed-in window. The
recipient, subject and thread come from the email being answered, never from the page.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.today import INBOX_FRESH_SECONDS
from app.assistant.replies import address_of, reply_subject

GROUPS = ("urgent", "reply", "fyi", "newsletter")


class MessageRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class DraftRequest(MessageRef):
    name: str = Field(default="", max_length=80)


class SendRequest(MessageRef):
    body: str = Field(min_length=1, max_length=20000)
    follow_up: bool = False


def follow_up_due(now: datetime) -> datetime:
    """Three working days later at 17:00."""
    due, added = now, 0
    while added < 3:
        due += timedelta(days=1)
        if due.weekday() < 5:
            added += 1
    return due.replace(hour=17, minute=0, second=0, microsecond=0)


def install(app: FastAPI, authorize) -> None:
    def bridge(request: Request):
        return request.app.state.agent

    def gmail_parts(request: Request):
        agent = bridge(request)
        inbox = getattr(agent, "inbox", None)
        if inbox is None:
            raise HTTPException(409, "Connect Gmail in Connections first.")
        return agent, inbox

    def find(inbox, message_id: str) -> tuple[str, dict]:
        last = getattr(inbox, "last", None)
        for group in GROUPS:
            for item in last[1]["groups"].get(group, []) if last else []:
                if item["message_id"] == message_id:
                    return group, item
        raise HTTPException(404, "That email isn't in the sorted inbox anymore. Refresh.")

    @app.get("/api/v1/inbox", dependencies=[Depends(authorize)])
    async def inbox(request: Request):
        agent = bridge(request)
        sorted_inbox = getattr(agent, "inbox", None)
        last = getattr(sorted_inbox, "last", None)
        if last is None:
            return {"gmail": sorted_inbox is not None, "sorted": None}
        at, data = last
        return {
            "gmail": True,
            "sorted": {
                "at": at,
                "fresh": time.time() - at < INBOX_FRESH_SECONDS,
                "has_more": data.get("has_more", False),
                "groups": {
                    group: [
                        {
                            key: item.get(key)
                            for key in (
                                "message_id",
                                "thread_id",
                                "from",
                                "subject",
                                "date",
                                "summary",
                                "action",
                            )
                        }
                        for item in data["groups"].get(group, [])
                    ]
                    for group in GROUPS
                },
            },
        }

    @app.post("/api/v1/inbox/open", dependencies=[Depends(authorize)])
    async def open_message(payload: MessageRef, request: Request):
        agent, sorted_inbox = gmail_parts(request)
        _group, item = find(sorted_inbox, payload.message_id)
        try:
            message = await asyncio.wait_for(agent.replies.message(item), timeout=30)
        except TimeoutError:
            raise HTTPException(504, "Gmail took too long. Try again.") from None
        except Exception as exc:
            raise HTTPException(502, str(exc) or "Couldn't open that email.") from None
        return {
            **message,
            "to": address_of(item["from"]),
            "subject": reply_subject(item["subject"]),
            "can_reply": bool(address_of(item["from"]) and item.get("reply_message_id")),
        }

    @app.post("/api/v1/inbox/draft", dependencies=[Depends(authorize)])
    async def draft(payload: DraftRequest, request: Request):
        agent, sorted_inbox = gmail_parts(request)
        _group, item = find(sorted_inbox, payload.message_id)
        try:
            body = await asyncio.wait_for(agent.replies.draft(item, payload.name), timeout=60)
        except TimeoutError:
            raise HTTPException(504, "Writing the draft took too long. Try again.") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception:
            raise HTTPException(502, "Couldn't write a draft. Check your connection.") from None
        return {"body": body}

    @app.post("/api/v1/inbox/send", dependencies=[Depends(authorize)])
    async def send(payload: SendRequest, request: Request):
        agent, sorted_inbox = gmail_parts(request)
        _group, item = find(sorted_inbox, payload.message_id)
        to = address_of(item["from"])
        if not to or not item.get("reply_message_id") or not item.get("thread_id"):
            raise HTTPException(422, "This email can't be answered from Bridge. Reply in Gmail.")
        args = SimpleNamespace(
            account_id=None,
            to=[to],
            subject=reply_subject(item["subject"]),
            body=payload.body,
            thread_id=item["thread_id"],
            in_reply_to=item["reply_message_id"],
        )
        try:
            sent = await agent.gmail.send(args)
        except Exception as exc:
            raise HTTPException(502, str(exc) or "Gmail didn't accept the reply.") from None
        followup = None
        if payload.follow_up:
            name = item["from"].split("<")[0].strip().strip('"') or to
            due = follow_up_due(datetime.now().astimezone())
            made = agent.proactive_store.add_followup(
                name, to, item["subject"][:120], due.isoformat()
            )
            followup = {"name": made.name, "due": made.due}
        agent.replies.drafts.pop(item["message_id"], None)
        return {"sent": True, "to": to, "message_id": sent.get("message_id"), "followup": followup}
