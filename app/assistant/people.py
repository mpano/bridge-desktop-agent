"""People: who you work with, what's between you, and a brief before you meet.

Bridge learns the people you work with from what it already sees: promises (who owes whom),
meetings (who was invited and who took on what), the email and Slack it sorted for you, your
calendar, and pull requests waiting for your review. One person can have several names,
email addresses and handles; you can merge two that are the same, add your own notes, or
forget someone (they're never tracked again).

A profile shows what's open between you both ways, meetings together (past and coming up),
recent messages and reviews, your notes, and a short "working on together" summary. Before a
meeting, the brief says what matters about the people in it. Everything is kept on this Mac;
only the summary and the brief send titles and short summaries (never whole messages) to
OpenAI.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.llm.structured import ask_json
from app.llm.usage import task

COLLECT_EVERY = 30 * 60
SUMMARY_HOURS = 6
SELF_WORDS = {"me", "you", "myself", "i", "the team", "team", "everyone", "all"}
# Senders that are machines, not colleagues (GitHub, Jira, calendar invites, newsletters…).
AUTOMATED = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|notifications?|notify|alerts?|mailer-daemon|"
    r"postmaster|bounces?|bot|robot|automated|news(letter)?|digest|updates?|calendar-"
    r"notification|jira|github|builds?|ci)([+._-].*)?@",
    re.I,
)
SERVICE_DOMAINS = ("github.com", "atlassian.net", "slack.com", "noreply.", "mailchimp")


def automated(name: str, email: str) -> bool:
    email = (email or "").lower()
    if email and (
        AUTOMATED.match(email) or any(d in email.split("@")[-1] for d in SERVICE_DOMAINS)
    ):
        return True
    return normal(name) in {"github", "jira", "slack", "google calendar", "confluence", "linear"}


TOGETHER = """You help the user keep track of a colleague. From the facts below (promises
between them, meetings, messages, reviews, the user's own notes), write JSON:
{"summary": "2-3 sentences: what the user and this person are working on together and
where things stand", "next": "one short sentence: the most useful next step, or empty"}
Use only the facts. Be specific (names of projects, tickets, meetings)."""

BRIEF = """You prepare the user for a meeting that's about to start. For each person in it
you get what's open between them and the user, recent meetings and messages. Reply with JSON:
{"brief": ["at most 4 short bullets: what's useful to know or bring up, most important
first, e.g. 'Sam still owes you the login fix (due Friday)', 'Last time you agreed to ship
onboarding on Friday'"]}
Use only the facts; skip people with nothing notable. No greetings."""


def normal(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s@.'-]", " ", name or "").lower().split())


def split_from(value: str) -> tuple[str, str]:
    """'Olivier Mupenzi <o@x.com>' -> ('Olivier Mupenzi', 'o@x.com')."""
    name, email = parseaddr(value or "")
    name = " ".join(name.strip().strip('"').split())
    email = email.lower() if "@" in email else ""
    if not name and email:
        name = email.split("@")[0].replace(".", " ").title()
    return name, email


class PeopleStore:
    COLUMNS = "id, name, emails, handles, note, hidden, first_seen, last_seen, seen, summary"

    def __init__(self, path: Path | str):
        self.path = str(path)
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS people (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "emails TEXT NOT NULL DEFAULT '[]', handles TEXT NOT NULL DEFAULT '[]', "
                "note TEXT NOT NULL DEFAULT '', hidden INTEGER NOT NULL DEFAULT 0, "
                "first_seen REAL NOT NULL, last_seen REAL NOT NULL, "
                "seen TEXT NOT NULL DEFAULT '{}', summary TEXT)"
            )

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    @staticmethod
    def _row(r) -> dict:
        return {
            "id": r[0],
            "name": r[1],
            "emails": json.loads(r[2]),
            "handles": json.loads(r[3]),
            "note": r[4],
            "hidden": bool(r[5]),
            "first_seen": r[6],
            "last_seen": r[7],
            "seen": json.loads(r[8]),
            "summary": json.loads(r[9]) if r[9] else None,
        }

    def all(self, *, hidden: bool = False) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                f"SELECT {self.COLUMNS} FROM people "
                + ("" if hidden else "WHERE hidden = 0 ")
                + "ORDER BY last_seen DESC"
            ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, person_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute(
                f"SELECT {self.COLUMNS} FROM people WHERE id = ?", (person_id,)
            ).fetchone()
        return self._row(row) if row else None

    def save(self, p: dict) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO people VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    p["id"],
                    p["name"],
                    json.dumps(sorted(set(p["emails"]))),
                    json.dumps(sorted(set(p["handles"]))),
                    p.get("note", ""),
                    int(p.get("hidden", False)),
                    p["first_seen"],
                    p["last_seen"],
                    json.dumps(p.get("seen", {})),
                    json.dumps(p["summary"]) if p.get("summary") else None,
                ),
            )

    def delete(self, person_id: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM people WHERE id = ?", (person_id,))


class People:
    def __init__(self, path, *, llm=None, agent=None, me=None, clock=None):
        self.store = PeopleStore(path)
        self.llm, self.agent = llm, agent
        self.me_lookup = me or (lambda: ())  # Your own names and addresses: never a "person".
        self.me: set[str] = set()
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._collected = 0.0
        self._briefs: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    # Who is who ---------------------------------------------------------------------------------

    def _mine(self, name: str, email: str) -> bool:
        return bool({normal(name), normal(email)} & self.me) or normal(name) in SELF_WORDS

    def match(self, person: dict, name: str = "", email: str = "", handle: str = "") -> bool:
        if email and email.lower() in person["emails"]:
            return True
        if handle and handle.lower() in person["handles"]:
            return True
        if name:
            wanted = normal(name)
            known = normal(person["name"])
            if wanted == known:
                return True
            # "Sam" matches "Sam Lee" (and "Sam Lee" an earlier "Sam") when it's the only Sam.
            if " " not in wanted:
                return known.split(" ")[0] == wanted
            return " " not in known and wanted.split(" ")[0] == known
        return False

    def _find_one(self, people: list[dict], name: str, email: str, handle: str) -> dict | None:
        for person in people:  # Exact address or handle first.
            if (email and email in person["emails"]) or (handle and handle in person["handles"]):
                return person
        named = [p for p in people if name and normal(p["name"]) == normal(name)]
        if len(named) == 1:
            return named[0]
        first = [p for p in people if name and self.match(p, name=name)]
        if len(first) != 1:
            return None
        # A first-name match never joins two people who have different addresses.
        if email and first[0]["emails"] and email not in first[0]["emails"]:
            return None
        return first[0]

    def _upsert(
        self,
        people: list[dict],
        name: str,
        email: str = "",
        handle: str = "",
        source: str = "",
        when: float | None = None,
    ) -> None:
        name, email, handle = name.strip(), email.strip().lower(), handle.strip().lower()
        if not (name or email or handle) or self._mine(name, email):
            return
        if automated(name, email):
            return
        when = when or time.time()
        found = self._find_one(people, name, email, handle)
        hidden = [p for p in self.store.all(hidden=True) if p["hidden"]]
        if found is None and any(self.match(p, name, email, handle) for p in hidden):
            return  # You asked Bridge to forget them.
        if found is None:
            found = {
                "id": uuid4().hex[:12],
                "name": name or (email.split("@")[0] if email else handle),
                "emails": [],
                "handles": [],
                "note": "",
                "hidden": False,
                "first_seen": when,
                "last_seen": when,
                "seen": {},
                "summary": None,
            }
            people.append(found)
        if email and email not in found["emails"]:
            found["emails"].append(email)
        if handle and handle not in found["handles"]:
            found["handles"].append(handle)
        # A full name is better than a first name or an address.
        if name and (" " in name and " " not in found["name"] or "@" in found["name"]):
            found["name"] = name
        found["last_seen"] = max(found["last_seen"], when)
        if source:
            found["seen"][source] = found["seen"].get(source, 0) + 1
        found["_dirty"] = True

    async def collect(self, force: bool = False) -> int:
        """Learn people from promises, meetings, mail, Slack, the calendar and GitHub."""
        if self._lock.locked():
            async with self._lock:  # One is already running: wait for it, don't skip it.
                return 0
        if not force and time.time() - self._collected < COLLECT_EVERY:
            return 0
        async with self._lock:
            self._collected = time.time()
            try:
                self.me = {normal(m) for m in self.me_lookup() if m}
            except Exception:
                pass
            people = self.store.all()
            for name, email, handle, source, when in await self._sightings():
                self._upsert(people, name, email, handle, source, when)
            changed = [p for p in people if p.pop("_dirty", False)]
            for person in changed:
                self.store.save(person)
            return len(changed)

    async def _sightings(self) -> list[tuple]:
        found: list[tuple] = []
        agent = self.agent
        commitments = getattr(agent, "commitments", None)
        if commitments is not None:
            for item in commitments.store.items(None):
                for name in re.split(r",| and ", item["person"] or ""):
                    found.append((name.strip(), "", "", "promises", item.get("said_at")))
        meetings = getattr(agent, "meetings", None)
        if meetings is not None:
            for m in meetings.store.recent(60):
                for p in m["event"].get("attendees", []):
                    found.append(
                        (p.get("name", ""), p.get("email", ""), "", "meetings", m["started"])
                    )
                for a in (m.get("summary") or {}).get("actions") or []:
                    if a.get("owner") == "them" and a.get("person") not in {"the team", ""}:
                        found.append((a["person"], "", "", "meetings", m["started"]))
        for source_name, source in (
            ("email", getattr(agent, "inbox", None)),
            ("slack", getattr(agent, "slack_inbox", None)),
        ):
            last = getattr(source, "last", None) if source is not None else None
            for group in last[1]["groups"].values() if last else []:
                for item in group:
                    if group is last[1]["groups"].get("newsletter"):
                        continue
                    name, email = split_from(item.get("from", ""))
                    found.append((name, email, "", source_name, last[0]))
        calendar = getattr(agent, "calendar", None)
        if calendar is not None:
            now = self.clock()
            window = SimpleNamespace(
                start=(now - timedelta(days=14)).isoformat(),
                end=(now + timedelta(days=7)).isoformat(),
            )
            try:
                events = (await asyncio.wait_for(calendar.events(window), 8))["events"]
            except Exception:
                events = []
            for e in events:
                for p in e.get("attendees") or []:
                    found.append(
                        (p.get("name", ""), p.get("email", ""), "", "calendar", time.time())
                    )
        github = getattr(agent, "github", None)
        try:
            if github is not None and await github.connected():
                for pr in await github.reviews():
                    if pr.get("author"):
                        found.append((pr["author"], "", pr["author"], "github", time.time()))
        except Exception:
            pass
        return found

    # A person ----------------------------------------------------------------------------------

    def find(self, query: str) -> list[dict]:
        words = normal(query).split()
        people = self.store.all()
        if not words:
            return people
        return [
            p
            for p in people
            if all(
                any(w in normal(field) for field in (p["name"], *p["emails"], *p["handles"]))
                for w in words
            )
        ]

    def _theirs(self, person: dict, name: str) -> bool:
        return any(
            self.match(person, name=part.strip()) for part in re.split(r",| and ", name or "")
        )

    def _with(self, person: dict, attendees: list[dict]) -> bool:
        return any(
            self.match(person, name=a.get("name", ""), email=a.get("email", "")) for a in attendees
        )

    async def facts(self, person: dict) -> dict:
        """Everything Bridge knows about you and this person, from its own data."""
        agent = self.agent
        open_items = {"you_owe": [], "they_owe": []}
        commitments = getattr(agent, "commitments", None)
        if commitments is not None:
            for item in commitments.store.items("open"):
                if not self._theirs(person, item["person"]):
                    continue
                entry = {
                    "id": item["id"],
                    "what": item["what"],
                    "due": item.get("due"),
                    "source": item["source"],
                }
                open_items["you_owe" if item["direction"] == "mine" else "they_owe"].append(entry)
        meetings_together = []
        meetings = getattr(agent, "meetings", None)
        if meetings is not None:
            for m in meetings.store.recent(60):
                actions = (m.get("summary") or {}).get("actions") or []
                theirs = [a for a in actions if self._theirs(person, a.get("person", ""))]
                if self._with(person, m["event"].get("attendees", [])) or theirs:
                    meetings_together.append(
                        {
                            "id": m["id"],
                            "title": m["title"],
                            "when": m["started"],
                            "summary": ((m.get("summary") or {}).get("summary") or "")[:300],
                            "their_actions": [a["what"] for a in theirs][:4],
                        }
                    )
        messages = []
        for source_name, source in (
            ("email", getattr(agent, "inbox", None)),
            ("slack", getattr(agent, "slack_inbox", None)),
        ):
            last = getattr(source, "last", None) if source is not None else None
            for group in last[1]["groups"].values() if last else []:
                for item in group:
                    name, email = split_from(item.get("from", ""))
                    if self.match(person, name=name, email=email):
                        messages.append(
                            {
                                "source": source_name,
                                "subject": item.get("subject", ""),
                                "summary": item.get("summary", "")[:200],
                                "date": item.get("date", ""),
                            }
                        )
        upcoming = []
        calendar = getattr(agent, "calendar", None)
        if calendar is not None:
            now = self.clock()
            window = SimpleNamespace(
                start=now.isoformat(), end=(now + timedelta(days=7)).isoformat()
            )
            try:
                events = (await asyncio.wait_for(calendar.events(window), 8))["events"]
            except Exception:
                events = []
            for e in events:
                if self._with(person, e.get("attendees") or []):
                    upcoming.append({"title": e.get("title") or "Meeting", "start": e["start"]})
        reviews = []
        github = getattr(agent, "github", None)
        try:
            if github is not None and person["handles"] and await github.connected():
                for pr in await github.reviews():
                    if pr.get("author", "").lower() in person["handles"]:
                        reviews.append(
                            {
                                "title": pr["title"],
                                "repo": pr["repo"],
                                "url": pr["url"],
                                "number": pr["number"],
                            }
                        )
        except Exception:
            pass
        return {
            "open": open_items,
            "meetings": meetings_together[:8],
            "upcoming": upcoming[:5],
            "messages": messages[:8],
            "reviews": reviews[:6],
        }

    async def profile(self, person_id: str) -> dict:
        person = self.store.get(person_id)
        if person is None:
            raise ValueError("Bridge doesn't know that person (anymore).")
        facts = await self.facts(person)
        return {**self.view(person), "facts": facts}

    async def together(self, person_id: str, *, fresh: bool = False) -> dict:
        """A short "working on together" summary (kept for a few hours)."""
        person = self.store.get(person_id)
        if person is None:
            raise ValueError("Bridge doesn't know that person (anymore).")
        cached = person.get("summary")
        if cached and not fresh and time.time() - cached.get("at", 0) < SUMMARY_HOURS * 3600:
            return cached
        facts = await self.facts(person)
        empty = (
            not any(
                [
                    *facts["open"].values(),
                    facts["meetings"],
                    facts["messages"],
                    facts["reviews"],
                    facts["upcoming"],
                ]
            )
            and not person["note"]
        )
        if empty or self.llm is None:
            summary = {"summary": "", "next": "", "at": time.time()}
        else:
            with task("people"):
                found = await ask_json(
                    self.llm,
                    TOGETHER,
                    {
                        "person": person["name"],
                        "your_note": person["note"],
                        **self._for_model(facts),
                    },
                )
            found = found if isinstance(found, dict) else {}
            summary = {
                "summary": str(found.get("summary") or "")[:600],
                "next": str(found.get("next") or "")[:200],
                "at": time.time(),
            }
        person["summary"] = summary
        self.store.save(person)
        return summary

    @staticmethod
    def _for_model(facts: dict) -> dict:
        """Titles and short summaries only; links and ids stay here."""
        return {
            "you_owe_them": [i["what"] for i in facts["open"]["you_owe"]],
            "they_owe_you": [
                i["what"] + (f" (due {i['due']})" if i["due"] else "")
                for i in facts["open"]["they_owe"]
            ],
            "meetings": [
                {"title": m["title"], "summary": m["summary"], "their_actions": m["their_actions"]}
                for m in facts["meetings"][:4]
            ],
            "messages": [
                {"subject": m["subject"], "summary": m["summary"]} for m in facts["messages"][:5]
            ],
            "reviews_waiting_on_you": [r["title"] for r in facts["reviews"]],
            "upcoming": [u["title"] for u in facts["upcoming"]],
        }

    @staticmethod
    def view(person: dict) -> dict:
        return {
            "id": person["id"],
            "name": person["name"],
            "emails": person["emails"],
            "handles": person["handles"],
            "note": person["note"],
            "last_seen": person["last_seen"],
            "seen": person["seen"],
            "summary": person.get("summary"),
        }

    def overview(self) -> list[dict]:
        return [self.view(p) for p in self.store.all()]

    def note(self, person_id: str, text: str) -> dict:
        person = self.store.get(person_id)
        if person is None:
            raise ValueError("Bridge doesn't know that person (anymore).")
        person["note"] = text.strip()[:2000]
        person["summary"] = None  # Rewritten with the note next time.
        self.store.save(person)
        return self.view(person)

    def rename(self, person_id: str, name: str) -> dict:
        person = self.store.get(person_id)
        if person is None:
            raise ValueError("Bridge doesn't know that person (anymore).")
        person["name"] = " ".join(name.split())[:80]
        self.store.save(person)
        return self.view(person)

    def merge(self, keep_id: str, other_id: str) -> dict:
        keep, other = self.store.get(keep_id), self.store.get(other_id)
        if keep is None or other is None or keep_id == other_id:
            raise ValueError("Choose two different people.")
        keep["emails"] += other["emails"]
        keep["handles"] += other["handles"]
        keep["note"] = "\n".join(n for n in (keep["note"], other["note"]) if n)
        keep["first_seen"] = min(keep["first_seen"], other["first_seen"])
        keep["last_seen"] = max(keep["last_seen"], other["last_seen"])
        for source, count in other["seen"].items():
            keep["seen"][source] = keep["seen"].get(source, 0) + count
        keep["summary"] = None
        self.store.save(keep)
        self.store.delete(other_id)
        return self.view(keep)

    def forget(self, person_id: str) -> dict:
        """Forget what Bridge learned and never track them again (only the name and addresses
        stay, so they can be recognized and left out)."""
        person = self.store.get(person_id)
        if person is None:
            raise ValueError("Already forgotten.")
        person.update(note="", summary=None, seen={}, hidden=True)
        self.store.save(person)
        return {"forgotten": person["name"]}

    # Before a meeting --------------------------------------------------------------------------

    async def brief(self, event: dict) -> dict:
        """What's useful to know about the people in this meeting."""
        key = f"{event.get('id')}:{event.get('start')}"
        if key in self._briefs:
            return self._briefs[key]
        await self.collect()
        people = []
        for attendee in event.get("attendees") or []:
            found = self._find_one(
                self.store.all(),
                attendee.get("name", ""),
                (attendee.get("email") or "").lower(),
                "",
            )
            if found is not None:
                people.append(found)
        rows = []
        for person in people[:8]:
            facts = await self.facts(person)
            rows.append(
                {"id": person["id"], "name": person["name"], "note": person["note"], "facts": facts}
            )
        notable = [
            r
            for r in rows
            if r["note"] or any(r["facts"]["open"].values()) or r["facts"]["meetings"]
        ]
        bullets: list[str] = []
        if notable and self.llm is not None:
            with task("people"):
                found = await ask_json(
                    self.llm,
                    BRIEF,
                    {
                        "meeting": event.get("title", ""),
                        "people": [
                            {
                                "name": r["name"],
                                "your_note": r["note"],
                                **self._for_model(r["facts"]),
                            }
                            for r in notable
                        ],
                    },
                )
            if isinstance(found, dict) and isinstance(found.get("brief"), list):
                bullets = [" ".join(str(b).split())[:200] for b in found["brief"]][:4]
        elif notable:
            for r in notable:
                for item in r["facts"]["open"]["they_owe"][:1]:
                    bullets.append(f"{r['name']} owes you: {item['what']}")
                for item in r["facts"]["open"]["you_owe"][:1]:
                    bullets.append(f"You owe {r['name']}: {item['what']}")
        result = {
            "event": {
                "id": event.get("id"),
                "title": event.get("title", ""),
                "start": event.get("start"),
            },
            "people": [{"id": r["id"], "name": r["name"]} for r in rows],
            "brief": bullets,
            "at": time.time(),
        }
        self._briefs[key] = result
        if len(self._briefs) > 30:
            self._briefs.pop(next(iter(self._briefs)))
        return result

    async def next_brief(self, hours: int = 3) -> dict | None:
        """The brief for your next meeting with people Bridge knows, in the next few hours."""
        calendar = getattr(self.agent, "calendar", None)
        if calendar is None:
            return None
        now = self.clock()
        window = SimpleNamespace(
            start=now.isoformat(), end=(now + timedelta(hours=hours)).isoformat()
        )
        try:
            events = (await asyncio.wait_for(calendar.events(window), 8))["events"]
        except Exception:
            return None
        for e in sorted(events, key=lambda e: e["start"]):
            if e.get("all_day") or not e.get("attendees"):
                continue
            if datetime.fromisoformat(e["start"]) < now - timedelta(minutes=5):
                continue
            found = await self.brief(e)
            if found["people"]:
                return found
        return None

    async def check(self) -> None:
        """From the proactive loop: keep learning who's who."""
        try:
            await self.collect()
        except Exception:
            pass
