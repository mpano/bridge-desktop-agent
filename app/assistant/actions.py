"""What Bridge did for you, and Undo where it's possible.

Every action that sends or changes something (a Slack message, a Jira move, a calendar event,
a draft, a recipe…) is written down in plain words when it happens. Where the service allows
it, Undo puts things back for a day afterwards: the Slack message is deleted, the ticket moves
back, the comment, event or draft is deleted, focus stops, the recipe or watcher is removed.
Some things can't be undone (an email that was sent is gone) and the list says so. Undo is your
own click, so it runs without asking again.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from app.integrations.providers import GOOGLE
from app.llm.models import ToolCall

UNDO_HOURS = 24
KEEP_DAYS = 30


def _short(text, limit: int = 80) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _who(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value[:3]) + (" …" if len(value) > 3 else "")
    return str(value or "")


# tool: (what it did, in words; how to undo it or None; why not, when it can't be)
TRACKED = {
    "slack_send_message": (
        lambda a, r: (
            f"Posted in {r.get('destination') or a.get('channel')}: “{_short(a.get('text'))}”"
        ),
        "slack_delete",
        "",
    ),
    "email_send": (
        lambda a, r: (
            f"Emailed {_who(r.get('to') or a.get('to'))}: “{_short(a.get('subject'), 60)}”"
        ),
        None,
        "An email can't be unsent once it's delivered.",
    ),
    "email_create_draft": (
        lambda a, r: f"Saved a Gmail draft: “{_short(a.get('subject'), 60)}”",
        "draft_delete",
        "",
    ),
    "messages_send": (
        lambda a, r: f"Sent a message to {a.get('to_name') or _who(a.get('to'))}",
        None,
        "Messages can't be unsent from Bridge.",
    ),
    "jira_comment": (
        lambda a, r: f"Commented on {r.get('key') or a.get('key')}: “{_short(a.get('text'))}”",
        "jira_comment_delete",
        "",
    ),
    "jira_move": (
        lambda a, r: (
            f"Moved {r.get('key') or a.get('key')}"
            + (f" from {r['from_status']}" if r.get("from_status") else "")
            + f" to {r.get('status') or a.get('status')}"
        ),
        "jira_move_back",
        "",
    ),
    "jira_create": (
        lambda a, r: f"Created {r.get('key', 'a Jira issue')}: “{_short(a.get('summary'), 60)}”",
        None,
        "Delete it in Jira if you don't need it.",
    ),
    "github_comment": (
        lambda a, r: (
            f"Commented on {r.get('repo') or a.get('repo')}#{r.get('number') or a.get('number')}"
            f": “{_short(a.get('body'))}”"
        ),
        "github_comment_delete",
        "",
    ),
    "github_create_issue": (
        lambda a, r: (
            f"Opened an issue in {r.get('repo') or a.get('repo')}: “{_short(a.get('title'), 60)}”"
        ),
        None,
        "Close it on GitHub if you don't need it.",
    ),
    "calendar_create_event": (
        lambda a, r: f"Added “{_short(a.get('title'), 60)}” to your calendar",
        "calendar_delete",
        "",
    ),
    "mac_calendar_create_event": (
        lambda a, r: f"Added “{_short(a.get('title'), 60)}” to your calendar",
        "mac_calendar_delete",
        "",
    ),
    "calendar_delete_event": (
        lambda a, r: f"Deleted “{_short(a.get('title'), 60)}” from your calendar",
        None,
        "Add it again if you need it.",
    ),
    "reminders_add": (
        lambda a, r: f"Added a reminder: “{_short(a.get('title'), 60)}”",
        None,
        "Remove it in Reminders if you don't need it.",
    ),
    "notes_create": (
        lambda a, r: f"Created a note: “{_short(a.get('title'), 60)}”",
        None,
        "Delete it in Notes if you don't need it.",
    ),
    "focus_start": (
        lambda a, r: f"Started focus for {a.get('minutes', '')} minutes",
        "focus_stop",
        "",
    ),
    "recipe_create": (
        lambda a, r: f"Created the recipe “{_short(r.get('name'), 60)}”",
        "recipe_delete",
        "",
    ),
    "watch_create": (
        lambda a, r: f"Started watching: {_short(r.get('label'), 60)}",
        "watch_delete",
        "",
    ),
    "operate_mac": (
        lambda a, r: f"Used your Mac: “{_short(a.get('task'), 70)}”",
        None,
        "Steps on your Mac can't be undone automatically.",
    ),
}


class Actions:
    def __init__(self, path: Path | str, agent=None):
        self.path, self.agent = str(path), agent
        self._pruned = 0.0
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS bridge_actions (id TEXT PRIMARY KEY, at REAL NOT NULL, "
                "tool TEXT NOT NULL, summary TEXT NOT NULL, undo TEXT, why_not TEXT, "
                "args TEXT NOT NULL, result TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'done', "
                "undone_at REAL, request_id TEXT)"
            )

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    # Writing it down --------------------------------------------------------------------------

    def record(self, tool: str, arguments: dict, result: dict, request_id: str = "") -> dict | None:
        """After an action succeeded: what it was, and whether it can be undone."""
        found = TRACKED.get(tool)
        if found is None:
            return None
        describe, undo, why_not = found
        result = result if isinstance(result, dict) else {}
        try:
            summary = describe(arguments, result)
        except Exception:
            summary = tool.replace("_", " ").capitalize()
        action_id = uuid4().hex[:12]
        keep = {k: v for k, v in result.items() if isinstance(v, (str, int, float, bool))}
        with self._db() as db:
            if time.time() - self._pruned > 3600:
                self._pruned = time.time()
                db.execute(
                    "DELETE FROM bridge_actions WHERE at < ?", (time.time() - KEEP_DAYS * 86400,)
                )
            db.execute(
                "INSERT INTO bridge_actions (id, at, tool, summary, undo, why_not, args, result, "
                "request_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    action_id,
                    time.time(),
                    tool,
                    summary[:300],
                    undo,
                    why_not,
                    json.dumps(arguments, default=str)[:6000],
                    json.dumps(keep, default=str)[:4000],
                    request_id,
                ),
            )
        return {"id": action_id, "undoable": bool(undo)}

    # Reading ----------------------------------------------------------------------------------

    def _row(self, row) -> dict:
        keys = (
            "id",
            "at",
            "tool",
            "summary",
            "undo",
            "why_not",
            "args",
            "result",
            "status",
            "undone_at",
            "request_id",
        )
        item = dict(zip(keys, row, strict=True))
        item["args"], item["result"] = json.loads(item["args"]), json.loads(item["result"])
        return item

    def get(self, action_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM bridge_actions WHERE id = ?", (action_id,)).fetchone()
        return self._row(row) if row else None

    def view(self, item: dict) -> dict:
        fresh = time.time() - item["at"] < UNDO_HOURS * 3600
        can = bool(item["undo"]) and item["status"] == "done" and fresh
        reason = item["why_not"] or ("" if fresh else "Too long ago to undo from Bridge.")
        return {
            "id": item["id"],
            "at": item["at"],
            "tool": item["tool"],
            "summary": item["summary"],
            "status": item["status"],
            "undone_at": item["undone_at"],
            "can_undo": can,
            "why_not": "" if can or item["status"] != "done" else reason,
            "url": item["result"].get("url") or "",
        }

    def recent(self, limit: int = 60) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM bridge_actions ORDER BY at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self.view(self._row(r)) for r in rows]

    # Undoing ----------------------------------------------------------------------------------

    async def undo(self, action_id: str) -> dict:
        item = self.get(action_id)
        if item is None:
            raise ValueError("That action is gone.")
        shown = self.view(item)
        if not shown["can_undo"]:
            raise ValueError(shown["why_not"] or "This one can't be undone.")
        with self._db() as db:
            claimed = db.execute(
                "UPDATE bridge_actions SET status = 'undoing' WHERE id = ? AND status = 'done'",
                (action_id,),
            ).rowcount
        if not claimed:
            raise ValueError("It's already being undone.")
        try:
            await self._undo(item)
        except Exception as exc:
            with self._db() as db:
                db.execute("UPDATE bridge_actions SET status = 'done' WHERE id = ?", (action_id,))
            message = str(exc) if isinstance(exc, ValueError) else ""
            raise ValueError(message or "Couldn't undo it. It may have changed since.") from None
        with self._db() as db:
            db.execute(
                "UPDATE bridge_actions SET status = 'undone', undone_at = ? WHERE id = ?",
                (time.time(), action_id),
            )
        return self.view(self.get(action_id))

    async def _tool(self, name: str, arguments: dict) -> dict:
        """Undo with one of Bridge's own tools (you clicked Undo: that's the OK)."""
        result = await self.agent.executor.execute(
            ToolCall(call_id=uuid4().hex, name=name, arguments=arguments), "undo", approved=True
        )
        if result.get("status") != "completed":
            raise ValueError(result.get("error") or "It didn't work.")
        return result.get("result") or {}

    async def _undo(self, item: dict) -> None:
        kind, args, result = item["undo"], item["args"], item["result"]
        agent = self.agent
        if kind == "slack_delete":
            if not (result.get("channel_id") and result.get("message_ts")):
                raise ValueError("Slack didn't say which message it was.")
            await agent.accounts.request(
                args.get("account_id"),
                "slack",
                "POST",
                "chat.delete",
                scopes=("chat:write",),
                json={"channel": result["channel_id"], "ts": result["message_ts"]},
            )
        elif kind == "draft_delete":
            await agent.accounts.request(
                args.get("account_id"),
                "gmail",
                "DELETE",
                "drafts/" + quote(str(result["draft_id"]), safe=""),
                scopes=(GOOGLE + "gmail.compose",),
            )
        elif kind == "jira_comment_delete":
            if not result.get("comment_id"):
                raise ValueError("Jira didn't say which comment it was.")
            key, comment = quote(result["key"], safe=""), quote(result["comment_id"], safe="")
            await agent.jira.c.request(
                "jira", "DELETE", f"issue/{key}/comment/{comment}", write=True
            )
        elif kind == "jira_move_back":
            if not result.get("from_status"):
                raise ValueError("Bridge doesn't know where the ticket was before.")
            await agent.jira.move(result["key"], result["from_status"])
        elif kind == "github_comment_delete":
            if not result.get("comment_id"):
                raise ValueError("GitHub didn't say which comment it was.")
            await agent.github.c.request(
                "github",
                "DELETE",
                f"repos/{result['repo']}/issues/comments/{int(result['comment_id'])}",
                write=True,
            )
        elif kind == "calendar_delete":
            await self._tool(
                "calendar_delete_event",
                {
                    "title": args["title"],
                    "start": args["start"],
                    "end": args["end"],
                    **({"calendar_id": args["calendar_id"]} if args.get("calendar_id") else {}),
                    **({"account_id": args["account_id"]} if args.get("account_id") else {}),
                },
            )
        elif kind == "mac_calendar_delete":
            await self._tool(
                "mac_calendar_delete_event",
                {"title": args["title"], "start": args["start"], "end": args["end"]},
            )
        elif kind == "focus_stop":
            await self._tool("focus_stop", {})
        elif kind == "recipe_delete":
            agent.recipes.delete(int(result["id"]))
        elif kind == "watch_delete":
            if not agent.proactive_store.remove_watch(int(result["id"])):
                raise ValueError("That watcher was already removed.")
        else:
            raise ValueError("This one can't be undone.")
