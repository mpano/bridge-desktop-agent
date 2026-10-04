"""Tools: promises you made and promises made to you (list, add, mark done)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class ListInput(Input):
    direction: Literal["all", "mine", "theirs"] = Field(
        default="all",
        description="mine = what the user promised; theirs = what others promised the user",
    )
    person: str = Field(
        default="", max_length=80, description="Only promises involving this person"
    )


class AddInput(Input):
    direction: Literal["mine", "theirs"] = Field(
        description="mine = the user promised it; theirs = someone promised it to the user"
    )
    person: str = Field(min_length=1, max_length=80, description="Who it was promised to or by")
    what: str = Field(min_length=3, max_length=140, description="The promised action, briefly")
    due: date | None = Field(default=None, description="When it's due, if known")


class DoneInput(Input):
    what: str = Field(
        min_length=3, max_length=140, description="Words from the promise, to find it"
    )


class CommitmentController:
    def __init__(self, tracker):
        self.tracker = tracker

    async def list(self, args):
        wanted = args.person.lower().strip()
        items = [
            {key: c[key] for key in ("direction", "person", "what", "due")}
            for c in self.tracker.store.items("open")
            if (args.direction == "all" or c["direction"] == args.direction)
            and (not wanted or wanted in c["person"].lower())
        ]
        return {"commitments": items, "today": date.today().isoformat()}

    async def add(self, args):
        added = self.tracker.store.add(
            {
                "message": f"manual-{date.today().isoformat()}",
                "direction": args.direction,
                "person": args.person.strip(),
                "what": args.what.strip(),
                "due": args.due.isoformat() if args.due else None,
                "source": "manual",
                "ref": {},
                "quote": "",
            }
        )
        return {"added": bool(added), **args.model_dump(mode="json")}

    async def done(self, args):
        words = [w for w in args.what.lower().split() if len(w) > 2]
        best, score = None, 0
        for c in self.tracker.store.items("open"):
            text = f"{c['what']} {c['person']}".lower()
            hits = sum(w in text for w in words)
            if hits > score:
                best, score = c, hits
        if best is None or score < max(1, len(words) // 2):
            return {"done": False}
        self.tracker.store.update(best["id"], status="done", closed_by="you")
        return {"done": True, "what": best["what"], "person": best["person"]}


def render_list(data: dict) -> str:
    items = data["commitments"]
    if not items:
        return "No open promises."
    mine = [c for c in items if c["direction"] == "mine"]
    theirs = [c for c in items if c["direction"] == "theirs"]

    def line(c):
        due = ""
        if c["due"]:
            due = " — overdue" if c["due"] < data["today"] else f" — due {c['due']}"
        return f"• {c['what']} ({c['person']}){due}"

    parts = []
    if mine:
        parts.append("You owe:\n" + "\n".join(line(c) for c in mine))
    if theirs:
        parts.append("Waiting on:\n" + "\n".join(line(c) for c in theirs))
    return "\n\n".join(parts)


def render_add(data: dict) -> str:
    if not data["added"]:
        return "That promise is already on the list."
    who = f"to {data['person']}" if data["direction"] == "mine" else f"from {data['person']}"
    due = f", due {data['due']}" if data.get("due") else ""
    return f"Added: {data['what']} ({who}{due}). It's on Today."


def render_done(data: dict) -> str:
    if not data["done"]:
        return "I couldn't find that promise. Check the list on Today."
    return f"✓ Marked done: {data['what']} ({data['person']})."


def register(registry, tracker) -> None:
    controller = CommitmentController(tracker)
    registry.register(
        Tool(
            "commitments_list",
            "List open promises: what the user promised people (with due dates) and what "
            "others promised the user. Found in the user's email and Slack, or added by them.",
            ListInput,
            RiskLevel.SAFE,
            controller.list,
            render=render_list,
        )
    )
    registry.register(
        Tool(
            "commitment_add",
            "Track a promise the user made or someone made to the user, e.g. 'remind me I "
            "promised Sam the budget by Friday'.",
            AddInput,
            RiskLevel.SAFE,
            controller.add,
            render=render_add,
        )
    )
    registry.register(
        Tool(
            "commitment_done",
            "Mark an open promise as done.",
            DoneInput,
            RiskLevel.SAFE,
            controller.done,
            render=render_done,
        )
    )
