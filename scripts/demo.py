"""Bridge with sample data, for screenshots and trying the UI.

    .venv/bin/python scripts/demo.py [port]

Runs Bridge's dashboard on http://localhost:<port> (8800 by default) with a throwaway
database full of realistic sample data: meetings, promises, people, watchers, recipes, a weekly
report, an inbox. It never touches your real Keychain, accounts, calendar, mail, microphone or
screen, sends nothing anywhere (the "AI" answers with canned text), and leaves nothing behind.
It prints a one-time link that signs you in.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def isolate(folder: Path) -> None:
    """Everything that could reach your real Mac or accounts is replaced."""
    from app import bootstrap
    from app.assistant import operator, talk
    from app.integrations import credentials, repos, tokens
    from app.llm import keychain
    from app.tools.system import notifications

    memory: dict = {}
    tokens.KeychainTokens.load = lambda self: dict(memory)
    tokens.KeychainTokens.save = lambda self, data: memory.update(data)
    tokens.GH_CANDIDATES = ()
    repos.find_copies = lambda home, extra=(), depth=3: {}
    credentials.KeychainCredentials.load = lambda self: {}
    credentials.KeychainCredentials.save = lambda self, *a, **k: None
    keychain.load_into = lambda settings: ""
    bootstrap.mac_hands = lambda: None
    bootstrap.meeting_ears = lambda: None
    operator.HUD = None
    talk.SAMPLES = folder / "voice-samples"

    async def quiet(*_args, **_kwargs):
        return None

    notifications.post_notification = quiet


class DemoCalendar:
    def __init__(self, now: datetime):
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        at = lambda h, m=0: day + timedelta(hours=h, minutes=m)  # noqa: E731
        soon = now + timedelta(minutes=25)
        soon = soon.replace(minute=(soon.minute // 5) * 5, second=0, microsecond=0)
        team = [
            {"name": "Sam Lee", "email": "sam@northwind.io"},
            {"name": "Ana Ruiz", "email": "ana@northwind.io"},
        ]
        self.items = [
            {
                "id": "e1",
                "title": "Standup",
                "start": at(9, 30),
                "end": at(9, 45),
                "attendees": team,
                "video": True,
                "location": "Zoom",
            },
            {
                "id": "e2",
                "title": "Design review: onboarding",
                "start": soon,
                "end": soon + timedelta(minutes=45),
                "attendees": team,
                "video": True,
                "location": "Google Meet",
            },
            {
                "id": "e3",
                "title": "1:1 with Olivier",
                "start": soon + timedelta(hours=2),
                "end": soon + timedelta(hours=2, minutes=30),
                "attendees": [{"name": "Olivier Mupenzi", "email": "olivier@northwind.io"}],
                "video": True,
                "location": "",
            },
            {
                "id": "e4",
                "title": "Focus: release notes",
                "start": at(17),
                "end": at(18),
                "attendees": [],
                "video": False,
                "location": "",
            },
        ]

    async def events(self, window):
        start = datetime.fromisoformat(window.start)
        end = datetime.fromisoformat(window.end)
        found = []
        for e in self.items:
            if e["end"] > start and e["start"] < end:
                found.append(
                    {
                        **e,
                        "start": e["start"].isoformat(),
                        "end": e["end"].isoformat(),
                        "all_day": False,
                        "calendar": "Work",
                        "busy": True,
                    }
                )
        return {"events": found}


class DemoLLM:
    """Canned answers, so the demo never calls OpenAI."""

    usage = None

    async def complete(self, instructions: str, text: str) -> str:
        lower = instructions.lower()
        if "three things that matter" in lower or "pick the three" in lower:
            return json.dumps(
                {"top": [], "headline": "A focused day: one review, then release notes."}
            )
        if "working on together" in lower:
            return json.dumps(
                {
                    "summary": "You and Sam are shipping the new onboarding together. "
                    "He's fixing the Safari login bug before Friday's release; you "
                    "owe him the onboarding deck.",
                    "next": "Send Sam the deck today.",
                }
            )
        if "prepare the user for a meeting" in lower:
            return json.dumps(
                {
                    "brief": [
                        "Sam still owes you the Safari login fix (due Friday)",
                        "You promised Ana the onboarding deck",
                        "Last time you agreed to ship onboarding on Friday",
                    ]
                }
            )
        if "weekly status update" in lower:
            return (
                "Done this week\n• Shipped faster sync (app#412) and the new settings page "
                "(app#418)\n• Fixed the export bug (NW-142) and closed 3 billing tickets\n"
                "• Sent Olivier the Q4 budget, as promised\n\nIn progress\n• Onboarding "
                "redesign (NW-151): copy review with Ana\n\nNext week\n• Ship onboarding "
                "on Friday"
            )
        return "Here's a short answer from the demo."

    async def generate_response(self, history, tools):
        from app.llm.models import LLMResponse

        return LLMResponse(
            [],
            "This is the demo, so I can't really do that — but in Bridge I'd take care of it.",
            [],
        )

    async def close(self):
        return None


def demo_inbox(now: float):
    groups = {
        "urgent": [
            {
                "message_id": "m1",
                "from": "Sam Lee <sam@northwind.io>",
                "subject": "Login bug on Safari",
                "summary": "Found the cause; needs your OK on the fix before Friday.",
                "date": "",
                "action": "Reply",
            }
        ],
        "reply": [
            {
                "message_id": "m2",
                "from": "Ana Ruiz <ana@northwind.io>",
                "subject": "Onboarding copy",
                "summary": "Can you send the deck so I can review the wording?",
                "date": "",
                "action": "Send the deck",
            },
            {
                "message_id": "m3",
                "from": "Olivier Mupenzi <olivier@northwind.io>",
                "subject": "Q4 budget",
                "summary": "Thanks for the numbers — one question about hiring.",
                "date": "",
                "action": "",
            },
        ],
        "fyi": [
            {
                "message_id": "m4",
                "from": "GitHub <noreply@github.com>",
                "subject": "[northwind/app] PR #418 merged",
                "summary": "Your PR was merged.",
                "date": "",
                "action": "",
            }
        ],
        "newsletter": [
            {
                "message_id": "m5",
                "from": "Product Weekly <news@pw.io>",
                "subject": "This week in product",
                "summary": "",
                "date": "",
                "action": "",
            }
        ],
    }
    return SimpleNamespace(last=(now, {"groups": groups, "total": 5, "days": 2}))


def seed(agent, now: datetime) -> None:
    from app.auth.store import AuthStore

    db = agent.proactive_store.path
    AuthStore(db).create_owner("demo@northwind.io", "Mpano", None)
    agent.proactive_store.update_settings(onboarded=True, morning_plan=True, evening_summary=True)
    store = agent.commitments.store
    for i, (direction, person, what, due, source) in enumerate(
        [
            (
                "mine",
                "Ana Ruiz",
                "Send the onboarding deck",
                (now + timedelta(days=1)).date(),
                "email",
            ),
            (
                "mine",
                "Olivier Mupenzi",
                "Share the hiring plan",
                (now - timedelta(days=1)).date(),
                "slack",
            ),
            (
                "theirs",
                "Sam",
                "Fix the Safari login bug",
                (now + timedelta(days=3)).date(),
                "meeting",
            ),
            ("theirs", "Dana Kim", "Review the pricing page", None, "email"),
        ]
    ):
        store.add(
            {
                "message": f"demo{i}",
                "direction": direction,
                "person": person,
                "what": what,
                "due": due.isoformat() if due else None,
                "source": source,
                "ref": {},
                "quote": what,
            }
        )
    started = time.time() - 26 * 3600
    agent.meetings.store.save(
        {
            "id": "d1a2b3c4d5e6",
            "title": "Weekly sync",
            "status": "done",
            "event": {
                "attendees": [
                    {"name": "Sam Lee", "email": "sam@northwind.io"},
                    {"name": "Ana Ruiz", "email": "ana@northwind.io"},
                ]
            },
            "started": started,
            "ended": started + 1900,
            "notes": [],
            "sent": [],
            "promises": ["x"],
            "segments": [
                {
                    "t": 12,
                    "end": 18,
                    "who": "Others",
                    "text": "Onboarding is ready apart from the copy.",
                },
                {"t": 20, "end": 26, "who": "You", "text": "I'll send the deck to Ana tomorrow."},
                {
                    "t": 31,
                    "end": 38,
                    "who": "Others",
                    "text": "I'll fix the Safari login bug before Friday.",
                },
                {"t": 44, "end": 48, "who": "You", "text": "Great, then we ship on Friday."},
            ],
            "summary": {
                "summary": "We reviewed the new onboarding. It's ready apart from the copy, which "
                "Ana will review. Sam fixes the Safari login bug, and we ship Friday.",
                "decisions": ["Ship the new onboarding on Friday", "Keep the 3-step layout"],
                "actions": [
                    {
                        "owner": "you",
                        "person": "you",
                        "what": "Send the deck to Ana",
                        "due": None,
                        "quote": "I'll send the deck to Ana tomorrow.",
                    },
                    {
                        "owner": "them",
                        "person": "Sam",
                        "what": "Fix the Safari login bug",
                        "due": None,
                        "quote": "I'll fix the Safari login bug before Friday.",
                    },
                ],
                "questions": ["Who owns the analytics events?"],
                "followup": {
                    "subject": "Weekly sync — next steps",
                    "body": "Hi Sam, hi Ana,\n\nThanks for today. We ship the new "
                    "onboarding on Friday.\n\n• I'll send Ana the deck\n• Sam "
                    "fixes the Safari login bug\n\nThanks!",
                },
            },
        }
    )
    ps = agent.proactive_store
    w = ps.add_watch(
        "jira",
        "NW-151 moves to Review",
        "NW-151 in review",
        target={"key": "NW-151", "when": "status_to", "status": "Review"},
        once=True,
    )
    ps.observed(w.id, {"status": "In Progress"})
    w2 = ps.add_watch(
        "github_pr",
        "northwind/app#420 is merged",
        "PR 420 merged",
        target={"repo": "northwind/app", "number": 420, "when": "merged"},
        once=True,
    )
    ps.happened(w2.id, "✓ PR #420 was merged by sam — Dark mode · northwind/app", done=True)
    ps.add_watch("email", "from:olivier", "Olivier replies", once=True)
    trigger = ps.add_watch(
        "github_mine",
        "Any pull request of yours is merged",
        "Announce merges",
        target={"event": "merged"},
    )
    ps.observed(trigger.id, {"seen": []})
    with agent.recipes._db() as conn:
        conn.execute(
            "INSERT INTO recipes (name, watch_id, trigger, actions, mode, runs, last_run, "
            "last_status, created) VALUES (?, ?, ?, ?, 'auto', 7, ?, 'done', ?)",
            (
                "Announce merges",
                trigger.id,
                json.dumps({"kind": "github_mine", "event": "merged"}),
                json.dumps([{"kind": "slack", "channel": "#releases", "text": "🚀 {title} {url}"}]),
                time.time() - 3600,
                time.time() - 9 * 86400,
            ),
        )
        conn.execute(
            "INSERT INTO recipe_runs (recipe_id, event, calls, status, created) "
            "VALUES (1, ?, ?, 'waiting', ?)",
            (
                json.dumps(
                    {
                        "headline": "✓ app#421 was merged",
                        "detail": "Faster search",
                        "url": "https://example.com",
                    }
                ),
                json.dumps(
                    [
                        {
                            "tool": "slack_send_message",
                            "arguments": {
                                "channel": "#releases",
                                "text": "🚀 Faster search https://example.com",
                            },
                        }
                    ]
                ),
                time.time() - 600,
            ),
        )
    agent.recipes._owned = None


def main() -> None:
    import asyncio

    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8800
    folder = Path(tempfile.mkdtemp(prefix="bridge-demo-"))
    isolate(folder)

    from app.api.launch import LaunchTickets
    from app.api.server import create_app
    from app.bootstrap import build_agent
    from app.config.settings import Settings

    settings = Settings(
        _env_file=None,
        database_path=folder / "demo.db",
        api_token="demo-" + folder.name,
        openai_api_key="sk-demo-not-real",
        talk_enabled=True,
        operator_enabled=True,
        meetings_enabled=True,
    )
    now = datetime.now().astimezone()
    agent = build_agent(settings, llm=DemoLLM())
    agent.scheduler = agent.proactive = None  # No background work, no notifications.
    agent.screen = None  # Never peek at your real screen.
    agent.calendar = DemoCalendar(now)
    agent.inbox = demo_inbox(time.time())
    for part in (agent.people, agent.meetings, agent.reports, agent.buddy):
        if hasattr(part, "calendar"):
            part.calendar = agent.calendar
    agent.briefs.calendar = agent.calendar
    agent.briefs.inbox = agent.inbox
    seed(agent, now)
    asyncio.run(agent.reports.write())

    tickets = LaunchTickets()
    app = create_app(settings, agent, enable_ui=True, launch_tickets=tickets)
    print(
        f"\nBridge demo (sample data only): http://localhost:{port}/#launch={tickets.issue()}\n",
        flush=True,
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
