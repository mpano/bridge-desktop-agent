"""One box for every automation: what kind did you describe?

"tell me when PR 412 merges" is a watcher; "when my PR merges, post it in #releases" is a
recipe; "every Friday at 5, tell me what slipped" is a scheduled request. Clear phrasing is
decided by rules (free and instant); only unclear wording asks the model.
"""

from __future__ import annotations

import re

from app.llm.usage import task

WATCH = re.compile(
    r"^\s*(please\s+)?(tell|let|notify|ping|alert|remind)\s+me\s+(know\s+)?(when|whenever|if|once)\b"
    r"|^\s*(watch|keep an eye on)\b",
    re.I,
)
SCHEDULE = re.compile(
    r"\b(every|each)\s+(day|morning|evening|night|week|weekday|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|month|hour|\d+\s*(minutes?|hours?|days?))\b"
    r"|\b(daily|weekly|monthly|hourly)\b|\bon\s+(mondays|tuesdays|wednesdays|thursdays|fridays)\b",
    re.I,
)
MOMENT = re.compile(
    r"\bremind me\b.*\bwhen(?:ever)? (?:i (?:open|launch|switch to|join|start|get back|come back)"
    r"|i'?m back|i am back|(?:the |my )?[\w' -]+ (?:starts|begins))"
    r"|\bwhen(?:ever)? (?:i (?:open|launch|join)|i'?m back)\b.*\bremind me\b",
    re.I,
)
TRIGGER = re.compile(r"^\s*(when|whenever|each time|every time|if|once)\b", re.I)
ACTION = re.compile(
    r"\b(post|send|email|message|dm|comment|move|reply|add|create|remind|ask bridge|summari[sz]e|"
    r"announce|forward|assign|label|open|start)\b",
    re.I,
)
CLASSIFY = """Classify the user's automation request. Reply with one word:
watch   - they want to be told when something happens ("tell me when the build fails")
recipe  - when something happens, Bridge should do something ("when X, post/send/add Y")
schedule - something should happen at set times ("every Monday at 9, brief me")
moment  - remind them at a moment, not a time ("remind me … when I open Figma")"""


async def kind_of(text: str, llm=None) -> str:
    words = " ".join(text.split())
    if MOMENT.search(words):
        return "moment"
    if WATCH.search(words):
        return "watch"
    if TRIGGER.search(words):
        rest = re.split(r",|\bthen\b", words, maxsplit=1)
        action = rest[1] if len(rest) > 1 else words
        return "recipe" if ACTION.search(action) else "watch"
    if SCHEDULE.search(words):
        return "schedule"
    if llm is not None:
        try:
            with task("recipes"):
                answer = (await llm.complete(CLASSIFY, words)).strip().lower()
            for kind in ("recipe", "watch", "schedule", "moment"):
                if answer.startswith(kind):
                    return kind
        except Exception:
            pass
    return "schedule"  # Ask handles anything else, with its usual approvals.
