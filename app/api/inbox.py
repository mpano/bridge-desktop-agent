"""Routes for the Inbox screen: the sorted inbox, one email in full, a draft reply, send.

Send is the owner's own click on the exact text shown in the signed-in window. The
recipient, subject and thread come from the email being answered, never from the page.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
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


def sort_time(item: dict) -> float:
    """Newest first across email (RFC 2822 dates) and Slack (ISO dates)."""
    text = item.get("date") or ""
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        try:
            return parsedate_to_datetime(text).timestamp()
        except (TypeError, ValueError):
            return 0.0


LISTED = (
    "message_id",
    "source",
    "thread_id",
    "from",
    "subject",
    "date",
    "summary",
    "action",
    "permalink",
)


def install(app: FastAPI, authorize) -> None:
    def bridge(request: Request):
        return request.app.state.agent

    def sources(agent) -> list:
        return [
            source
            for source in (getattr(agent, "inbox", None), getattr(agent, "slack_inbox", None))
            if source is not None
        ]

    def find(agent, message_id: str) -> tuple[str, dict]:
        for source in sources(agent):
            last = getattr(source, "last", None)
            for group in GROUPS:
                for item in last[1]["groups"].get(group, []) if last else []:
                    if item["message_id"] == message_id:
                        return group, item
        raise HTTPException(404, "That message isn't in the sorted inbox anymore. Refresh.")

    def is_slack(item: dict) -> bool:
        return item.get("source") == "slack"

    @app.get("/api/v1/inbox", dependencies=[Depends(authorize)])
    async def inbox(request: Request):
        agent = bridge(request)
        connected = sources(agent)
        done = [source.last for source in connected if getattr(source, "last", None)]
        flags = {
            "gmail": getattr(agent, "inbox", None) is not None,
            "slack": getattr(agent, "slack_inbox", None) is not None,
        }
        if not done:
            return {**flags, "sorted": None}
        groups = {group: [] for group in GROUPS}
        for _at, data in done:
            for group in GROUPS:
                for item in data["groups"].get(group, []):
                    groups[group].append(
                        {key: item.get(key) for key in LISTED}
                        | {"source": item.get("source", "gmail")}
                    )
        oldest = min(at for at, _ in done)
        return {
            **flags,
            "sorted": {
                "at": max(at for at, _ in done),
                "fresh": time.time() - oldest < INBOX_FRESH_SECONDS,
                "has_more": any(data.get("has_more") for _, data in done),
                "groups": {
                    group: sorted(items, key=sort_time, reverse=True)
                    for group, items in groups.items()
                },
            },
        }

    @app.post("/api/v1/inbox/open", dependencies=[Depends(authorize)])
    async def open_message(payload: MessageRef, request: Request):
        agent = bridge(request)
        _group, item = find(agent, payload.message_id)
        if is_slack(item):
            earlier = await agent.slack_inbox.thread(item)
            return {
                "body": item.get("text", ""),
                "date": item.get("date", ""),
                "earlier": earlier,
                "to": item["subject"] + (" · in the thread" if item.get("thread_ts") else ""),
                "subject": item["subject"],
                "can_reply": bool(item.get("channel")),
                "source": "slack",
            }
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
            "source": "gmail",
        }

    @app.post("/api/v1/inbox/draft", dependencies=[Depends(authorize)])
    async def draft(payload: DraftRequest, request: Request):
        agent = bridge(request)
        _group, item = find(agent, payload.message_id)
        writer = agent.slack_inbox if is_slack(item) else agent.replies
        try:
            body = await asyncio.wait_for(writer.draft(item, payload.name), timeout=60)
        except TimeoutError:
            raise HTTPException(504, "Writing the draft took too long. Try again.") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception:
            raise HTTPException(502, "Couldn't write a draft. Check your connection.") from None
        return {"body": body}

    @app.post("/api/v1/inbox/send", dependencies=[Depends(authorize)])
    async def send(payload: SendRequest, request: Request):
        agent = bridge(request)
        _group, item = find(agent, payload.message_id)
        if is_slack(item):
            # The conversation and thread come from the message itself, never the page.
            try:
                await agent.slack_inbox.send(item, payload.body)
            except Exception as exc:
                raise HTTPException(502, str(exc) or "Slack didn't accept the reply.") from None
            return {"sent": True, "to": item["subject"], "followup": None}
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
