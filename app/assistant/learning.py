"""Bridge learns from your corrections.

It notices when something didn't go right — a reply draft you rewrote before sending, a
"promise" you said wasn't one, an Ask request that failed or that you gave 👎, an action you
declined, a Use-your-Mac task that didn't finish — and, once a week or when you ask, proposes
short lessons ("keep replies to Olivier under three sentences"). Each lesson is tested on your
past cases before you see it; only lessons you accept are used, in the part of Bridge they're
about, and you can turn any off. Cases stay on this Mac for 60 days; they're sent to the model
only to find and test lessons, like any other request.
"""

from __future__ import annotations

import difflib
import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from app.llm.structured import ask_json
from app.llm.usage import labelled

log = logging.getLogger(__name__)
AREAS = {
    "drafts": "Reply drafts",
    "promises": "Finding promises",
    "ask": "Answering in Ask",
    "actions": "Choosing actions",
    "operator": "Using your Mac",
}
KEEP_DAYS = 60
MIN_CASES = 2
MAX_RULES = 12  # Per area: lessons stay short enough to sit in a prompt.
EDITED = 0.92  # A sent reply this different from the draft counts as a correction.
PROPOSE = """You help an AI assistant called Bridge learn from its user's corrections. Below are
recent cases in one area where Bridge got something wrong or the user changed what it did,
plus the lessons Bridge already follows. Propose at most 3 NEW lessons that would have
prevented several of these cases. Each lesson is one short, concrete instruction to Bridge
(at most 25 words), general enough to help next time but not so broad that it changes
unrelated behaviour. Never put private details (addresses, numbers, secrets) in a lesson;
names are fine. If the cases don't share a pattern, propose nothing. Reply as JSON:
{"lessons": [{"rule": "...", "why": "one sentence", "cases": [<case ids>]}]}"""
CLASSIFY = """Decide for each quote whether it is a real promise (an explicit commitment to do a
specific thing later) under these rules. Reply as JSON:
{"verdicts": [{"id": <id>, "promise": true|false}]}"""
REWRITE = """Rewrite this draft reply so it follows the lesson below, changing nothing else.
Reply with only the rewritten text."""
JUDGE = """An assistant's lesson is checked against past cases where it went wrong. For each case,
say whether following the lesson would have led to a better result. Also say if the lesson is
too broad (would change unrelated behaviour). Reply as JSON:
{"helped": [<case ids it would have fixed>], "too_broad": true|false}"""


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, " ".join(a.split()), " ".join(b.split())).ratio()


class Learning:
    def __init__(self, path: Path | str, llm, settings=None, clock=time.time):
        self.path, self.llm, self.settings, self.clock = str(path), llm, settings, clock
        self.commitments = None  # For real promises to test against.
        self.notify = None
        self._active: dict[str, list[str]] | None = None
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS learn_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, area TEXT NOT NULL, kind TEXT NOT NULL,
                    at REAL NOT NULL, summary TEXT NOT NULL, detail TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS learn_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, area TEXT NOT NULL, rule TEXT NOT NULL,
                    why TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                    decided REAL, tested TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS learn_meta (key TEXT PRIMARY KEY, value TEXT);
                """
            )

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "learning_enabled", True))

    # Noticing ------------------------------------------------------------------------------

    def record(self, area: str, kind: str, summary: str, detail: dict | None = None) -> None:
        if not self.enabled or area not in AREAS:
            return
        detail = {k: (v[:2000] if isinstance(v, str) else v) for k, v in (detail or {}).items()}
        try:
            with self._db() as db:
                db.execute(
                    "INSERT INTO learn_cases (area, kind, at, summary, detail) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        area,
                        kind,
                        self.clock(),
                        " ".join(summary.split())[:200],
                        json.dumps(detail, ensure_ascii=False),
                    ),
                )
                db.execute(
                    "DELETE FROM learn_cases WHERE at < ?", (self.clock() - KEEP_DAYS * 86400,)
                )
        except sqlite3.Error:
            log.warning("Couldn't note a correction.")

    def draft_sent(self, channel: str, who: str, draft: str | None, sent: str) -> None:
        """A reply went out: if you rewrote Bridge's draft, that's a lesson to learn."""
        if draft and sent and similarity(draft, sent) < EDITED:
            self.record("drafts", channel, f"Reply to {who}", {"draft": draft, "sent": sent})

    def cases(self, area: str | None = None, unused: bool = False, since: float = 0) -> list[dict]:
        query = "SELECT id, area, kind, at, summary, detail, used FROM learn_cases WHERE at >= ?"
        params: list = [since]
        if area:
            query += " AND area = ?"
            params.append(area)
        if unused:
            query += " AND used = 0"
        with self._db() as db:
            rows = db.execute(query + " ORDER BY at DESC LIMIT 200", params).fetchall()
        keys = ("id", "area", "kind", "at", "summary", "detail", "used")
        items = [dict(zip(keys, row, strict=True)) for row in rows]
        for item in items:
            item["detail"] = json.loads(item["detail"])
        return items

    # Lessons in use ----------------------------------------------------------------------

    def rules(self, status: str | None = None) -> list[dict]:
        query = "SELECT id, area, rule, why, status, created, decided, tested FROM learn_rules"
        params: tuple = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        with self._db() as db:
            rows = db.execute(query + " ORDER BY created DESC", params).fetchall()
        keys = ("id", "area", "rule", "why", "status", "created", "decided", "tested")
        items = [dict(zip(keys, row, strict=True)) for row in rows]
        for item in items:
            item["tested"] = json.loads(item["tested"] or "{}")
            item["area_name"] = AREAS.get(item["area"], item["area"])
        return items

    def lessons(self, area: str) -> str:
        """Accepted lessons for one part of Bridge, ready to append to its instructions."""
        if self._active is None:
            self._active = {}
            for rule in self.rules("active"):
                self._active.setdefault(rule["area"], []).append(rule["rule"])
        rules = self._active.get(area) or []
        if not rules or not self.enabled:
            return ""
        return "\nLessons the user has taught you (follow them):\n" + "\n".join(
            f"- {r}" for r in rules
        )

    def decide(self, rule_id: int, status: str) -> None:
        if status not in {"active", "rejected", "off"}:
            raise ValueError("Choose accept, reject or turn off.")
        with self._db() as db:
            if not db.execute(
                "UPDATE learn_rules SET status = ?, decided = ? WHERE id = ?",
                (status, self.clock(), rule_id),
            ).rowcount:
                raise ValueError("That lesson is gone.")
        self._active = None

    def forget(self) -> None:
        with self._db() as db:
            db.execute("DELETE FROM learn_cases")
            db.execute("DELETE FROM learn_rules")
        self._active = None

    # Finding lessons -----------------------------------------------------------------------

    @labelled("learning")
    async def propose(self) -> int:
        """Look at new cases in each area and propose tested lessons. Returns how many."""
        made = 0
        for area in AREAS:
            fresh = self.cases(area, unused=True)
            if len(fresh) < MIN_CASES:
                continue
            existing = [
                r["rule"] for r in self.rules() if r["area"] == area and r["status"] != "rejected"
            ]
            if len([r for r in self.rules("active") if r["area"] == area]) >= MAX_RULES:
                continue
            shown = [
                {"id": c["id"], "kind": c["kind"], "summary": c["summary"], "detail": c["detail"]}
                for c in fresh[:20]
            ]
            try:
                answer = await ask_json(
                    self.llm,
                    PROPOSE,
                    {"area": AREAS[area], "cases": shown, "lessons_already_followed": existing},
                )
            except Exception:
                log.warning("Couldn't look for lessons in %s.", area)
                continue
            for lesson in (answer.get("lessons") if isinstance(answer, dict) else None) or []:
                rule = " ".join(str(lesson.get("rule") or "").split())[:200]
                if len(rule) < 8 or any(similarity(rule, old) > 0.8 for old in existing):
                    continue
                related = [c for c in fresh if c["id"] in set(lesson.get("cases") or [])] or fresh
                tested = await self.test(area, rule, related, existing)
                if tested.get("worse", 0) > tested.get("better", 0) or tested.get("too_broad"):
                    continue  # Didn't hold up on your past cases: don't bother you with it.
                with self._db() as db:
                    db.execute(
                        "INSERT INTO learn_rules (area, rule, why, status, created, tested) "
                        "VALUES (?, ?, ?, 'proposed', ?, ?)",
                        (
                            area,
                            rule,
                            " ".join(str(lesson.get("why") or "").split())[:200],
                            self.clock(),
                            json.dumps(tested),
                        ),
                    )
                existing.append(rule)
                made += 1
            with self._db() as db:
                db.executemany(
                    "UPDATE learn_cases SET used = 1 WHERE id = ?", [(c["id"],) for c in fresh]
                )
        return made

    async def test(self, area: str, rule: str, cases: list[dict], existing: list[str]) -> dict:
        """Check a lesson on your past cases before proposing it."""
        try:
            if area == "promises":
                return await self._test_promises(rule, cases, existing)
            if area == "drafts":
                return await self._test_drafts(rule, cases)
            return await self._judge(rule, cases)
        except Exception:
            log.warning("Couldn't test a lesson.", exc_info=True)
            return {"method": "untested", "cases": len(cases), "better": 0, "worse": 0}

    async def _test_promises(self, rule: str, cases: list[dict], existing: list[str]) -> dict:
        """Re-check the sentences you dismissed (should become 'not a promise') and real
        promises (should stay promises), without and with the lesson."""
        samples = [
            {"id": f"x{c['id']}", "quote": c["detail"].get("quote") or c["summary"], "real": False}
            for c in cases[:10]
        ]
        real = []
        if self.commitments is not None:
            for item in self.commitments.store.items(None)[:40]:
                if item["status"] != "dismissed" and item.get("quote"):
                    real.append({"id": f"r{item['id']}", "quote": item["quote"], "real": True})
        samples += real[:10]

        async def run(rules: list[str]) -> int:
            answer = await ask_json(
                self.llm,
                CLASSIFY,
                {
                    "rules": rules or ["(no extra rules)"],
                    "quotes": [{"id": s["id"], "quote": s["quote"]} for s in samples],
                },
            )
            verdicts = {
                str(v.get("id")): bool(v.get("promise")) for v in (answer or {}).get("verdicts", [])
            }
            return sum(verdicts.get(s["id"], True) == s["real"] for s in samples)

        before = await run(existing)
        after = await run([*existing, rule])
        return {
            "method": "replayed",
            "cases": len(samples),
            "before": before,
            "after": after,
            "better": max(0, after - before),
            "worse": max(0, before - after),
        }

    async def _test_drafts(self, rule: str, cases: list[dict]) -> dict:
        """Does the lesson bring the draft closer to what you actually sent?"""
        better = worse = 0
        checked = 0
        for case in cases[:6]:
            draft, sent = case["detail"].get("draft", ""), case["detail"].get("sent", "")
            if not draft or not sent:
                continue
            rewritten = (
                await self.llm.complete(
                    REWRITE, json.dumps({"lesson": rule, "draft": draft}, ensure_ascii=False)
                )
            ).strip()
            old, new = similarity(draft, sent), similarity(rewritten, sent)
            checked += 1
            if new > old + 0.02:
                better += 1
            elif new < old - 0.02:
                worse += 1
        return {"method": "replayed", "cases": checked, "better": better, "worse": worse}

    async def _judge(self, rule: str, cases: list[dict]) -> dict:
        answer = await ask_json(
            self.llm,
            JUDGE,
            {
                "lesson": rule,
                "cases": [
                    {"id": c["id"], "summary": c["summary"], "detail": c["detail"]}
                    for c in cases[:10]
                ],
            },
        )
        helped = set((answer or {}).get("helped") or [])
        shown = len(cases[:10])
        return {
            "method": "judged",
            "cases": shown,
            "better": len(helped & {c["id"] for c in cases}),
            "worse": 0,
            "too_broad": bool((answer or {}).get("too_broad")),
        }

    # The weekly report --------------------------------------------------------------------

    def week(self) -> dict:
        now = self.clock()
        this = self.cases(since=now - 7 * 86400)
        last = [c for c in self.cases(since=now - 14 * 86400) if c["at"] < now - 7 * 86400]

        def count(items, area, kind=None):
            return sum(
                1 for c in items if c["area"] == area and (kind is None or c["kind"] == kind)
            )

        lines = []
        for area, name, what in (
            ("drafts", "Drafts you rewrote", None),
            ("promises", "Promises you dismissed", None),
            ("ask", "Ask answers that missed", None),
            ("actions", "Actions you declined", None),
            ("operator", "Mac tasks that didn't finish", None),
        ):
            lines.append(
                {
                    "area": area,
                    "label": name,
                    "this_week": count(this, area, what),
                    "last_week": count(last, area, what),
                }
            )
        learned = [r for r in self.rules("active") if (r["decided"] or 0) >= now - 7 * 86400]
        return {
            "lines": lines,
            "learned": learned,
            "proposed": len(self.rules("proposed")),
            "active": len(self.rules("active")),
        }

    async def check(self) -> None:
        """Called by the proactive loop: on Fridays after 4 pm, find lessons and tell you."""
        if not self.enabled or self.notify is None:
            return
        now = datetime.fromtimestamp(self.clock())
        if now.weekday() != 4 or now.hour < 16:
            return
        week = now.strftime("%G-W%V")
        with self._db() as db:
            row = db.execute("SELECT value FROM learn_meta WHERE key = 'week'").fetchone()
            if row and row[0] == week:
                return  # Once a week.
            db.execute(
                "INSERT INTO learn_meta VALUES ('week', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (week,),
            )
        made = await self.propose()
        report = self.week()
        fixes = sum(line["this_week"] for line in report["lines"])
        if made or fixes:
            await self.notify(
                "What Bridge learned this week",
                (f"{made} new lesson{'s' if made != 1 else ''} to review. " if made else "")
                + f"{fixes} correction{'s' if fixes != 1 else ''} noted this week.",
                "settings",
            )
