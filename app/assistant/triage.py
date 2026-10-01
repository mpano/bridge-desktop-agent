"""Inbox triage: group recent unread email by what it needs from the user."""

from __future__ import annotations

import time
from types import SimpleNamespace

from app.llm.structured import ask_json

CATEGORIES = ("urgent", "reply", "fyi", "newsletter")
INSTRUCTIONS = """You triage the user's unread email. For each message decide one category:
- "urgent": time-sensitive or important and needs action today
- "reply": a real person is waiting for the user's answer
- "fyi": worth knowing, no action needed
- "newsletter": newsletters, promotions, automated notifications, receipts
Write a summary of at most 15 words saying what the sender wants, and for urgent/reply a
short suggested next step (at most 8 words). Reply as JSON:
{"items": [{"id": "<message id>", "category": "...", "summary": "...", "action": "..."}]}
Include every message exactly once, using its id."""


class InboxTriage:
    def __init__(self, gmail, llm):
        self.gmail, self.llm = gmail, llm
        self.last: tuple[float, dict] | None = None  # (time.time(), result) of the last run

    async def triage(self, days: int = 2, limit: int = 20) -> dict:
        found = await self.gmail.search(
            SimpleNamespace(
                account_id=None, query=f"in:inbox is:unread newer_than:{days}d", limit=limit
            )
        )
        messages = found["messages"]
        if not messages:
            empty = {"groups": {key: [] for key in CATEGORIES}, "total": 0, "days": days}
            self.last = (time.time(), empty)
            return empty
        data = [
            {
                "id": item["message_id"],
                "from": item["from"],
                "subject": item["subject"],
                "date": item["date"],
                "preview": item["snippet"],
            }
            for item in messages
        ]
        answer = await ask_json(self.llm, INSTRUCTIONS, data)
        by_id = {item["message_id"]: item for item in messages}
        groups: dict[str, list] = {key: [] for key in CATEGORIES}
        placed = set()
        for entry in answer.get("items", []) if isinstance(answer, dict) else []:
            message = by_id.get(str(entry.get("id")))
            if message is None or message["message_id"] in placed:
                continue  # Ignore anything the model invented.
            category = entry.get("category") if entry.get("category") in CATEGORIES else "fyi"
            placed.add(message["message_id"])
            groups[category].append(
                {
                    "message_id": message["message_id"],
                    "thread_id": message.get("thread_id"),
                    "reply_message_id": message.get("reply_message_id"),
                    "from": message["from"],
                    "subject": message["subject"],
                    "summary": str(entry.get("summary") or message["snippet"])[:200],
                    "action": str(entry.get("action") or "")[:100],
                }
            )
        for message in messages:  # Never drop an email because the model skipped it.
            if message["message_id"] not in placed:
                groups["fyi"].append(
                    {
                        "message_id": message["message_id"],
                        "thread_id": message.get("thread_id"),
                        "reply_message_id": message.get("reply_message_id"),
                        "from": message["from"],
                        "subject": message["subject"],
                        "summary": message["snippet"][:200],
                        "action": "",
                    }
                )
        result = {
            "groups": groups,
            "total": len(messages),
            "days": days,
            "has_more": found.get("has_more", False),
            "content_is_untrusted": True,
        }
        self.last = (time.time(), result)
        return result


def sender(value: str) -> str:
    name = value.split("<")[0].strip().strip('"')
    return name or value


def render(data: dict) -> str:
    if not data["total"]:
        return f"No unread email in the last {data['days']} days. 🎉"
    titles = {
        "urgent": "🔴 Urgent",
        "reply": "✉️ Needs your reply",
        "fyi": "📌 FYI",
        "newsletter": "🗞 Newsletters & notifications",
    }
    parts = []
    for key in ("urgent", "reply", "fyi"):
        items = data["groups"][key]
        if not items:
            continue
        rows = []
        for item in items:
            subject = item["subject"] or "(no subject)"
            line = f"• {sender(item['from'])} — {subject}\n  {item['summary']}"
            if item["action"]:
                line += f"  → {item['action']}"
            rows.append(line)
        parts.append(f"{titles[key]} ({len(items)})\n" + "\n".join(rows))
    newsletters = data["groups"]["newsletter"]
    if newsletters:
        names = ", ".join(sorted({sender(item["from"]) for item in newsletters})[:6])
        parts.append(f"{titles['newsletter']} ({len(newsletters)}): {names}")
    more = "\n\n…older unread mail not included." if data.get("has_more") else ""
    return "\n\n".join(parts) + more
