"""People as tools: "what's open with Sam?", "who is Daniel?", "prep me for my next meeting",
"remember that Ana prefers Slack"."""

from __future__ import annotations

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class PersonInput(Input):
    name: str = Field(min_length=1, max_length=80, description="Their name or email address")


class NoteInput(PersonInput):
    note: str = Field(min_length=2, max_length=500, description="What to remember about them")


def one(people, name: str) -> dict:
    found = people.find(name)
    if not found:
        raise ValueError(f"Bridge doesn't know anyone called “{name}” yet.")
    exact = [p for p in found if p["name"].lower() == name.lower().strip()]
    if len(found) > 1 and not exact:
        names = ", ".join(p["name"] for p in found[:5])
        raise ValueError(f"Several people match “{name}”: {names}. Which one?")
    return (exact or found)[0]


def render_person(d: dict) -> str:
    f = d["facts"]
    lines = [d["name"] + (f" · {', '.join(d['emails'][:2])}" if d["emails"] else "")]
    if d.get("together", {}).get("summary"):
        lines.append(d["together"]["summary"])
    for item in f["open"]["they_owe"]:
        lines.append(
            f"• They owe you: {item['what']}" + (f" (due {item['due']})" if item["due"] else "")
        )
    for item in f["open"]["you_owe"]:
        lines.append(
            f"• You owe them: {item['what']}" + (f" (due {item['due']})" if item["due"] else "")
        )
    for r in f["reviews"]:
        lines.append(f"• Waiting for your review: {r['title']}")
    for u in f["upcoming"][:2]:
        lines.append(f"• Next meeting together: {u['title']}")
    if d["note"]:
        lines.append(f"Your note: {d['note']}")
    if len(lines) == 1:
        lines.append("Nothing open between you right now.")
    return "\n".join(lines)


def register(registry, people) -> None:
    async def lookup(args):
        await people.collect()
        person = one(people, args.name)
        profile = await people.profile(person["id"])
        return {**profile, "together": await people.together(person["id"])}

    async def prep(_args):
        await people.collect()
        found = await people.next_brief(hours=12)
        if found is None:
            return {"title": "", "brief": [], "people": []}
        return {
            "title": found["event"]["title"],
            "brief": found["brief"],
            "people": found["people"],
        }

    async def remember(args):
        await people.collect()
        person = one(people, args.name)
        note = "\n".join(n for n in (person["note"], args.note.strip()) if n)
        return people.note(person["id"], note)

    registry.register(
        Tool(
            "person_lookup",
            "What's going on between the user and a colleague: what each owes the other, meetings "
            "together, recent messages, reviews waiting, notes. Use for 'what's open with Sam?', "
            "'who is Daniel?', 'what do I owe Ana?'.",
            PersonInput,
            RiskLevel.SAFE,
            lookup,
            render=render_person,
        )
    )
    registry.register(
        Tool(
            "meeting_prep",
            "Brief the user for their next meeting: what's useful to know about the people in it "
            "(open promises both ways, what was agreed last time). Use for 'prep me for my next "
            "meeting', 'anything I should know before the 3pm?'.",
            Input,
            RiskLevel.SAFE,
            prep,
            render=lambda d: (
                f"Before “{d['title']}”:\n" + "\n".join(f"• {b}" for b in d["brief"])
                if d["brief"]
                else (
                    f"Nothing notable for “{d['title']}”."
                    if d["title"]
                    else "No meetings with people Bridge knows in the next 12 hours."
                )
            ),
        )
    )
    registry.register(
        Tool(
            "person_note",
            "Remember something about a colleague (e.g. 'Ana prefers Slack', 'Sam is out next "
            "week'). It's shown on their profile and used in meeting briefs.",
            NoteInput,
            RiskLevel.SAFE,
            remember,
            render=lambda d: f"✓ I'll remember that about {d['name']}.",
        )
    )
