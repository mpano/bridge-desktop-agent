"""Tools: inbox triage, follow-up tracking, and planning the day."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.assistant import day_plan, triage
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.productivity.contacts import emails_from


class TriageInput(Input):
    days: int = Field(default=2, ge=1, le=14, description="How far back to look")
    limit: int = Field(default=20, ge=5, le=40)


class FollowupInput(Input):
    person: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[^\x00-\x1f\x7f,;<>]+$",
        description="Contact name or email address of the person who should reply",
    )
    due: str = Field(max_length=40, description="ISO 8601 date-time with UTC offset")
    about: str = Field(min_length=1, max_length=120, description="What the reply is about")
    email: str | None = Field(default=None, description="Filled in by Bridge from Contacts")

    @field_validator("due")
    @classmethod
    def offset(cls, value):
        if datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is None:
            raise ValueError("Use a full date-time with UTC offset.")
        return value


class FollowupCancelInput(Input):
    about: str = Field(min_length=1, max_length=120, description="Person or topic words")


class PlanInput(Input):
    day: Literal["today", "tomorrow"] = "today"


class PlanApplyInput(Input):
    plan_id: int = Field(ge=1, description="The plan_id from plan_day")
    blocks: list[dict] | None = Field(default=None, description="Filled in by Bridge")


class PlanningController:
    def __init__(
        self,
        store,
        planner=None,
        inbox=None,
        contacts=None,
        clock=lambda: datetime.now().astimezone(),
    ):
        self.store, self.planner, self.inbox, self.contacts = store, planner, inbox, contacts
        self.clock = clock

    # Triage -------------------------------------------------------------------------------

    async def triage(self, args):
        if self.inbox is None:
            raise ValueError("Connect Gmail in the dashboard's Connections page first.")
        return await self.inbox.triage(args.days, args.limit)

    # Follow-ups ---------------------------------------------------------------------------

    async def resolve_followup(self, args):
        """Bridge, not the model, decides the address: the person's email from Contacts."""
        [email] = await emails_from([args.person], self.contacts)
        return args.model_copy(update={"email": email})

    async def followup(self, args):
        if self.inbox is None:
            raise ValueError("Connect Gmail in the dashboard's Connections page first.")
        due = datetime.fromisoformat(args.due.replace("Z", "+00:00"))
        if due <= self.clock():
            raise ValueError("Choose a time in the future.")
        name = args.person if "@" not in args.person else args.email
        item = self.store.add_followup(name, args.email, args.about.strip(), due.isoformat())
        return {
            "id": item.id,
            "name": item.name,
            "email": item.email,
            "about": item.about,
            "due": item.due,
        }

    async def followups(self, _):
        return {
            "followups": [
                {
                    "id": f.id,
                    "name": f.name,
                    "email": f.email,
                    "about": f.about,
                    "due": f.due,
                    "status": f.status,
                }
                for f in self.store.followups()
                if f.status in {"waiting", "overdue", "replied"}
            ][-20:]
        }

    async def cancel_followup(self, args):
        words = args.about.casefold().split()
        matches = [
            f
            for f in self.store.followups()
            if f.status in {"waiting", "overdue"}
            and all(word in f"{f.name} {f.email} {f.about}".casefold() for word in words)
        ]
        if not matches:
            raise ValueError(f"No open follow-up matches “{args.about}”.")
        if len(matches) > 1:
            raise ValueError("Several follow-ups match. Be more specific.")
        self.store.set_followup(matches[0].id, "cancelled")
        return {"name": matches[0].name, "about": matches[0].about}

    # Plan my day --------------------------------------------------------------------------

    async def plan(self, args):
        if self.planner is None:
            raise ValueError("Day planning isn't available.")
        return await self.planner.plan(args.day)

    async def resolve_plan(self, args):
        """The approval shows the stored plan's exact blocks, whatever the model sent."""
        saved = self.store.plan(args.plan_id)
        if saved is None:
            raise ValueError("That plan has expired. Ask me to plan your day again.")
        _day, blocks = saved
        if not blocks:
            raise ValueError("That plan has no time blocks to add.")
        return args.model_copy(update={"blocks": blocks})

    async def apply_plan(self, args):
        return await self.planner.apply(args.blocks)


def _when(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%a %d %b %H:%M")


def render_followups(data: dict) -> str:
    if not data["followups"]:
        return "No follow-ups. Try “remind me if Olivier doesn't reply by Friday”."
    marks = {"waiting": "◷", "overdue": "⚠", "replied": "✓"}
    rows = []
    for item in data["followups"]:
        mark = marks.get(item["status"], "•")
        deadline = f"by {_when(item['due'])}, {item['status']}"
        rows.append(f"{mark} {item['name']} — {item['about']} ({deadline})")
    return "Follow-ups:\n" + "\n".join(rows)


def register(registry, controller: PlanningController, gmail_available: bool):
    if gmail_available:
        registry.register(
            Tool(
                "email_triage",
                "Sort recent unread email into urgent / needs reply / FYI / newsletters with a "
                "one-line summary each. Use for 'what needs my attention', 'triage my inbox'.",
                TriageInput,
                RiskLevel.SAFE,
                controller.triage,
                render=triage.render,
            )
        )
        registry.register(
            Tool(
                "followup_create",
                "Track that someone should reply: 'remind me if Olivier doesn't reply by "
                "Friday'. Bridge notifies when they reply, or at the deadline if they don't.",
                FollowupInput,
                RiskLevel.SAFE,
                controller.followup,
                resolve=controller.resolve_followup,
                render=lambda d: (
                    f"✓ I'll let you know if {d['name']} hasn't replied by "
                    f"{_when(d['due'])} ({d['about']})."
                ),
            )
        )
        registry.register(
            Tool(
                "followup_list",
                "List tracked follow-ups and whether people replied.",
                Input,
                RiskLevel.SAFE,
                controller.followups,
                render=render_followups,
            )
        )
        registry.register(
            Tool(
                "followup_cancel",
                "Stop tracking one follow-up, by person or topic words.",
                FollowupCancelInput,
                RiskLevel.SAFE,
                controller.cancel_followup,
                render=lambda d: f"✓ Stopped tracking: {d['name']} — {d['about']}",
            )
        )
    registry.register(
        Tool(
            "plan_day",
            "Plan today (or tomorrow) in time blocks around meetings, using due reminders and "
            "email that needs a reply. Returns a plan_id; nothing is added to the calendar.",
            PlanInput,
            RiskLevel.SAFE,
            controller.plan,
            render=day_plan.render,
        )
    )
    registry.register(
        Tool(
            "plan_day_apply",
            "Add a plan's time blocks to the calendar after approval. Give the plan_id from "
            "plan_day; Bridge fills in the exact blocks.",
            PlanApplyInput,
            RiskLevel.CONFIRM,
            controller.apply_plan,
            resolve=controller.resolve_plan,
            confirmation_message="Add these time blocks to your calendar?",
            render=lambda d: f"✓ Added {d['count']} blocks to your calendar.",
        )
    )
