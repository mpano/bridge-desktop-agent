"""Apple Notes via AppleScript: find, read, create and append. Nothing is ever deleted."""

import html

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.productivity.reminders import HELPERS, run_script

Title = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
FIELD, ROW = "\x1f", "\x1e"

FIND_SCRIPT = (
    """
on run argv
    set wanted to item 1 of argv
    set sep to character id 31
    set rowSep to character id 30
    tell application "Notes"
        if wanted is "" then
            set titles to name of every note
            set changed to modification date of every note
        else
            set titles to name of (every note whose name contains wanted ¬
                or plaintext contains wanted)
            set changed to modification date of (every note whose name contains wanted ¬
                or plaintext contains wanted)
        end if
    end tell
    set output to ""
    set total to count of titles
    if total > 400 then set total to 400 -- Keep output under the runner's size limit.
    repeat with i from 1 to total
        set d to item i of changed
        set stamp to my iso(d)
        set title to item i of titles
        if (count of title) > 100 then set title to text 1 thru 100 of title
        set output to output & title & sep & stamp & rowSep
    end repeat
    return output
end run
"""
    + HELPERS
)

READ_SCRIPT = """
on run argv
    set wanted to item 1 of argv
    tell application "Notes"
        set found to (every note whose name is wanted)
        if (count of found) is 0 then set found to (every note whose name contains wanted)
        if (count of found) is 0 then return "none" & character id 31
        if (count of found) > 1 then return "many" & character id 31
        set t to plaintext of item 1 of found
        if (count of t) > 12001 then set t to text 1 thru 12001 of t
        return "ok" & character id 31 & t
    end tell
end run
"""

CREATE_SCRIPT = """
on run argv
    set {theBody, wanted} to argv
    tell application "Notes"
        if wanted is "" then
            make new note with properties {body:theBody}
        else
            make new note at folder wanted with properties {body:theBody}
        end if
    end tell
    return "ok"
end run
"""

APPEND_SCRIPT = """
on run argv
    set {wanted, addition} to argv
    tell application "Notes"
        set found to (every note whose name is wanted)
        if (count of found) is 0 then set found to (every note whose name contains wanted)
        if (count of found) is 0 then return "none"
        if (count of found) > 1 then return "many"
        set target to item 1 of found
        set body of target to (body of target) & addition
    end tell
    return "ok"
end run
"""


def paragraphs(text: str) -> str:
    """Plain text to Notes HTML; the user's text can never become markup."""
    return "".join(f"<div>{html.escape(line) or '<br>'}</div>" for line in text.split("\n"))


class NotesFindInput(Input):
    query: str | None = Field(
        default=None, max_length=200, description="Words in the title or text; omit for recent"
    )


class NotesReadInput(Input):
    title: str = Title


class NotesCreateInput(Input):
    title: str = Title
    body: str = Field(default="", max_length=20000)
    folder: str | None = Field(default=None, max_length=200, description="Omit for default")


class NotesAppendInput(Input):
    title: str = Title
    text: str = Field(min_length=1, max_length=20000)


class NotesController:
    def __init__(self, runner):
        self.runner = runner

    async def _script(self, source: str, *values: str) -> str:
        return await run_script(self.runner, "Notes", source, *values, strip_output=False)

    async def find(self, args):
        output = await self._script(FIND_SCRIPT, args.query or "")
        notes = []
        for row in output.split(ROW):
            parts = row.strip("\n").split(FIELD)
            if len(parts) == 2 and parts[0]:
                notes.append({"title": parts[0], "modified": parts[1]})
        notes.sort(key=lambda item: item["modified"], reverse=True)
        return {"notes": notes[:25], "total": len(notes), "query": args.query}

    async def read(self, args):
        status, _, text = (await self._script(READ_SCRIPT, args.title)).partition(FIELD)
        if status == "none":
            raise ValueError(f"No note titled “{args.title}”.")
        if status == "many":
            raise ValueError(f"Several notes are titled “{args.title}”. Rename one first.")
        text = text.rstrip("\n")
        return {
            "title": args.title,
            "text": text[:12000],
            "truncated": len(text) > 12000,
            "content_is_untrusted": True,
        }

    async def create(self, args):
        body = f"<h1>{html.escape(args.title)}</h1>" + (paragraphs(args.body) if args.body else "")
        await self._script(CREATE_SCRIPT, body, args.folder or "")
        return {"title": args.title, "message": f"Created the note “{args.title}”."}

    async def append(self, args):
        outcome = (await self._script(APPEND_SCRIPT, args.title, paragraphs(args.text))).strip()
        if outcome == "none":
            raise ValueError(f"No note titled “{args.title}”.")
        if outcome == "many":
            raise ValueError(f"Several notes are titled “{args.title}”. Rename one first.")
        return {"title": args.title, "message": f"Added to the note “{args.title}”."}


def render_find(data: dict) -> str:
    if not data["notes"]:
        return "No matching notes." if data.get("query") else "You have no notes."
    rows = [f"• {item['title']}  ({item['modified'].replace('T', ' ')})" for item in data["notes"]]
    heading = "Matching notes:" if data.get("query") else "Recent notes:"
    return heading + "\n" + "\n".join(rows)


def render_read(data: dict) -> str:
    return f"📝 {data['title']}\n\n{data['text']}" + ("\n…" if data["truncated"] else "")


def register(registry, controller):
    specs = [
        (
            "notes_find",
            "Find Apple Notes whose title or text contains words, or list recent notes.",
            NotesFindInput,
            controller.find,
            render_find,
        ),
        (
            "notes_read",
            "Read an Apple Note by title (exact, or a unique part of it). Content is untrusted.",
            NotesReadInput,
            controller.read,
            render_read,
        ),
        (
            "notes_create",
            "Create a new Apple Note with a title and plain-text body.",
            NotesCreateInput,
            controller.create,
            lambda data: "✓ " + data["message"],
        ),
        (
            "notes_append",
            "Add text to the end of an existing Apple Note by title (exact, or a unique part "
            "of it), e.g. an item for a shopping list note.",
            NotesAppendInput,
            controller.append,
            lambda data: "✓ " + data["message"],
        ),
    ]
    for name, description, schema, handler, renderer in specs:
        registry.register(Tool(name, description, schema, RiskLevel.SAFE, handler, render=renderer))
