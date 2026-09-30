"""Apple Reminders via AppleScript. Values are osascript arguments, never script source."""

from datetime import datetime

from pydantic import Field, field_validator

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool

Text = Field(min_length=1, max_length=300, pattern=r"^[^\x00-\x1f\x7f]+$")
FIELD, ROW = "\x1f", "\x1e"  # ASCII unit/record separators survive titles with tabs or commas.

HELPERS = """
on iso(d)
    if d is missing value then return ""
    return (year of d as text) & "-" & my pad(month of d as integer) & "-" & my pad(day of d) ¬
        & "T" & my pad(hours of d) & ":" & my pad(minutes of d)
end iso
on pad(n)
    return text -2 thru -1 of ("0" & (n as text))
end pad
"""

LIST_SCRIPT = (
    """
on run argv
    set wanted to item 1 of argv
    set sep to character id 31
    set rowSep to character id 30
    set output to ""
    tell application "Reminders"
        if wanted is "" then
            set theLists to every list
        else
            set theLists to {list wanted}
        end if
        repeat with l in theLists
            set listName to name of l
            set titles to name of (every reminder of l whose completed is false)
            set dues to due date of (every reminder of l whose completed is false)
            repeat with i from 1 to count of titles
                set output to output & listName & sep & (item i of titles) & sep ¬
                    & my iso(item i of dues) & rowSep
            end repeat
        end repeat
    end tell
    return output
end run
"""
    + HELPERS
)

ADD_SCRIPT = """
on run argv
    set {theTitle, wanted, noteText, dueSeconds} to argv
    set dueDate to missing value
    if dueSeconds is not "" then set dueDate to (current date) + (dueSeconds as integer)
    tell application "Reminders"
        if wanted is "" then
            set target to default list
        else
            set target to list wanted
        end if
        set r to make new reminder at end of reminders of target with properties {name:theTitle}
        if noteText is not "" then set body of r to noteText
        if dueDate is not missing value then
            set due date of r to dueDate
            set remind me date of r to dueDate
        end if
        return name of target
    end tell
end run
"""

COMPLETE_SCRIPT = """
on run argv
    set {theTitle, wanted} to argv
    tell application "Reminders"
        if wanted is "" then
            set matches to (every reminder whose name is theTitle and completed is false)
        else
            set matches to (every reminder of list wanted ¬
                whose name is theTitle and completed is false)
        end if
        if (count of matches) is 0 then return "none"
        if (count of matches) > 1 then return "many"
        set completed of item 1 of matches to true
        return "done"
    end tell
end run
"""


async def run_script(runner, app: str, source: str, *values: str, **options) -> str:
    try:
        return await runner.run("/usr/bin/osascript", "-e", source, *values, **options)
    except TimeoutError:
        raise ValueError(
            f"{app} didn't respond. If macOS asked to let Bridge control {app}, click OK "
            "(or allow it in System Settings > Privacy & Security > Automation) and try again."
        ) from None


class ReminderListInput(Input):
    list_name: str | None = Field(default=None, max_length=200, description="Omit for all lists")


class ReminderAddInput(Input):
    title: str = Text
    due: str | None = Field(
        default=None,
        max_length=40,
        description="Optional ISO 8601 date-time with UTC offset; an alert fires then",
    )
    list_name: str | None = Field(default=None, max_length=200)
    notes: str = Field(default="", max_length=4000)

    @field_validator("due")
    @classmethod
    def offset_required(cls, value):
        if value is None:
            return value
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Use a full date-time with UTC offset.")
        return value


class ReminderCompleteInput(Input):
    title: str = Text
    list_name: str | None = Field(default=None, max_length=200)


STOPWORDS = {"a", "an", "the", "my", "to", "for", "of", "and", "reminder", "reminders"}


def significant(text: str) -> set[str]:
    words = "".join(c if c.isalnum() else " " for c in text.casefold()).split()
    return {word for word in words if word not in STOPWORDS}


class RemindersController:
    def __init__(self, runner, clock=lambda: datetime.now().astimezone()):
        self.runner, self.clock = runner, clock

    async def _script(self, source: str, *values: str) -> str:
        return await run_script(self.runner, "Reminders", source, *values)

    async def list(self, args):
        output = await self._script(LIST_SCRIPT, args.list_name or "")
        reminders = []
        for row in output.split(ROW):
            parts = row.strip("\n").split(FIELD)
            if len(parts) == 3 and parts[1]:
                reminders.append({"list": parts[0], "title": parts[1], "due": parts[2] or None})
        reminders.sort(key=lambda item: (item["due"] is None, item["due"] or "", item["title"]))
        return {"reminders": reminders[:100], "total": len(reminders)}

    async def add(self, args):
        seconds = ""
        if args.due:
            due = datetime.fromisoformat(args.due.replace("Z", "+00:00"))
            delta = round((due - self.clock()).total_seconds())
            if delta < -60:
                raise ValueError("That time is in the past. Choose a future time.")
            seconds = str(max(delta, 0))
        list_name = await self._script(
            ADD_SCRIPT, args.title, args.list_name or "", args.notes, seconds
        )
        return {
            "title": args.title,
            "list": list_name,
            "due": args.due,
            "message": f"Added “{args.title}” to Reminders ({list_name}).",
        }

    async def complete(self, args):
        """Complete the one open reminder matching exactly, or else by all its words."""
        open_items = (await self.list(ReminderListInput(list_name=args.list_name)))["reminders"]
        wanted = args.title.casefold().strip()
        matches = [item for item in open_items if item["title"].casefold().strip() == wanted]
        if not matches:
            words = significant(wanted)
            matches = [item for item in open_items if words and words <= significant(item["title"])]
        if not matches:
            raise ValueError(f"No open reminder matches “{args.title}”.")
        if len({(item["title"], item["list"]) for item in matches}) > 1:
            names = ", ".join(sorted({item["title"] for item in matches}))
            raise ValueError(f"Several open reminders match “{args.title}”: {names}. Be specific.")
        match = matches[0]
        outcome = await self._script(COMPLETE_SCRIPT, match["title"], match["list"])
        if outcome != "done":
            raise ValueError(f"Several open reminders are named “{match['title']}”. Name the list.")
        return {"title": match["title"], "message": f"Marked “{match['title']}” as done."}


def render_list(data: dict) -> str:
    if not data["reminders"]:
        return "No open reminders."
    rows = []
    for item in data["reminders"]:
        due = ""
        if item["due"]:
            due = " — due " + datetime.fromisoformat(item["due"]).strftime("%a %d %b %H:%M")
        rows.append(f"• {item['title']}{due}  ({item['list']})")
    more = f"\n…and {data['total'] - len(rows)} more." if data["total"] > len(rows) else ""
    return "Reminders:\n" + "\n".join(rows) + more


def register(registry, controller):
    registry.register(
        Tool(
            "reminders_list",
            "List open (not completed) Apple Reminders, soonest due first; optionally one list.",
            ReminderListInput,
            RiskLevel.SAFE,
            controller.list,
            render=render_list,
        )
    )
    registry.register(
        Tool(
            "reminders_add",
            "Add an Apple Reminder, e.g. 'remind me to call mom at 6pm'. With due, an alert "
            "fires at that time. Resolve relative times against the current date; if a time "
            "of day has already passed today, use the next occurrence (tomorrow).",
            ReminderAddInput,
            RiskLevel.SAFE,
            controller.add,
            render=lambda data: "✓ " + data["message"],
        )
    )
    registry.register(
        Tool(
            "reminders_complete",
            "Mark one open Apple Reminder as done. Give its title or its key words; "
            "ambiguous matches are refused.",
            ReminderCompleteInput,
            RiskLevel.SAFE,
            controller.complete,
            render=lambda data: "✓ " + data["message"],
        )
    )
