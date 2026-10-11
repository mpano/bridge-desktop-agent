"""Meeting notes as tools: "take notes", "stop the notes", "what did we decide?"."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.assistant.meetings import MeetingError
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class NotesInput(Input):
    action: Literal["start", "stop", "pause", "resume", "ask"] = Field(
        description="start or stop taking notes, pause/resume, or ask about the meeting now"
    )
    title: str = Field(default="", max_length=120, description="Optional meeting name")
    question: str = Field(default="", max_length=500, description="For ask")


class RecallInput(Input):
    question: str = Field(min_length=3, max_length=500)
    meeting: str = Field(
        default="", max_length=120, description="Words from the meeting's title; empty = latest"
    )


def register(registry, meetings) -> None:
    async def notes(args):
        try:
            if args.action == "start":
                found = await meetings.start(args.title)
                return {**found, "said": "started"}
            if args.action == "stop":
                return {**(await meetings.stop()), "said": "stopped"}
            if args.action in {"pause", "resume"}:
                return {**meetings.pause(args.action == "pause"), "said": args.action + "d"}
            return {"answer": await meetings.ask(args.question or "Summarize it so far.")}
        except MeetingError as exc:
            raise ValueError(str(exc)) from None

    async def recall(args):
        listed = meetings.overview()["meetings"]
        words = args.meeting.casefold().split()
        found = [m for m in listed if all(w in m["title"].casefold() for w in words)]
        if not found:
            raise ValueError("No meeting notes match that. Meetings are kept for a while only.")
        try:
            answer = await meetings.ask(args.question, found[0]["id"])
        except MeetingError as exc:
            raise ValueError(str(exc)) from None
        return {"meeting": found[0]["title"], "answer": answer}

    def show(data: dict) -> str:
        if "answer" in data:
            return data["answer"]
        said = data.get("said")
        if said == "started":
            extra = f" {data['problem']}" if data.get("problem") else ""
            return (
                f"● Taking notes: {data['title']}. Let everyone know you're taking notes. "
                f"Say “stop the notes” when it's over.{extra}"
            )
        if said == "stopped":
            return "Stopped. Writing the summary — it'll be in Meetings in a moment."
        return f"Notes {said}."

    registry.register(
        Tool(
            "meeting_notes",
            "Take notes of a meeting the user is in on this Mac: start (listens to their mic and "
            "the call's sound, transcribes on the Mac, live notes), pause, resume, stop (then a "
            "summary, decisions and action items into Promises), or ask about what was said so "
            "far ('what did Sam say about the deadline?').",
            NotesInput,
            RiskLevel.SAFE,
            notes,
            render=show,
        )
    )
    registry.register(
        Tool(
            "meeting_recall",
            "Answer a question about a past meeting Bridge took notes of ('what did we decide in "
            "the design review?', 'who's doing the migration?'). Uses the latest matching "
            "meeting's transcript.",
            RecallInput,
            RiskLevel.SAFE,
            recall,
            render=lambda d: f"{d['meeting']}: {d['answer']}",
        )
    )
