"""The proactive assistant: meeting heads-ups and "tell me when …" watches.

Runs while Bridge's local service is open. It only reads (calendar, mail search, Slack
search) and posts local notifications — no model call, nothing is sent anywhere, and it
never acts on what it finds.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.assistant.watchers import APP_EVERY, WatchError
from app.workflows.watches import ProactiveStore, Watch

MEETING_CHECK_SECONDS = 60
WATCH_CHECK_SECONDS = 120
MENTIONS = {"@me", "me", "mentions", "my mentions", "mentions of me"}


def slack_query(query: str, identity: str | None) -> str:
    """ "mentions" becomes a search for <@USERID>, using the connected Slack identity."""
    if query.strip().casefold() in MENTIONS and identity and "/" in identity:
        return f"<@{identity.split('/', 1)[1]}>"
    return query


class Proactive:
    def __init__(
        self,
        store: ProactiveStore,
        notify: Callable[[str, str], Awaitable[None]],
        calendar=None,
        gmail=None,
        slack=None,
        accounts=None,
        focus=None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ):
        self.store, self.notify = store, notify
        self.calendar, self.gmail, self.slack, self.accounts = calendar, gmail, slack, accounts
        self.clock = clock
        self.focus = focus
        self.commitments = None  # Promises you made and are owed; re-checked and nudged here.
        self.briefs = None  # The morning brief and evening wrap-up, on time.
        self.repos = None  # Teammates' pushes, releases and broken main in your repos.
        self.usage = None  # Budget alerts.
        self.learning = None  # Friday: what Bridge learned from your corrections.
        self.watchers = None  # GitHub, Jira and app watchers (app/assistant/watchers.py).
        self.reports = None  # Friday: the weekly report draft.
        self._last_watch_check = 0.0

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                if self.focus is not None:
                    await self.focus.check()  # Ends a focus session whose time passed.
                await self.check_meetings()
                if self.commitments is not None:
                    await self.commitments.tick()
                if self.briefs is not None:
                    await self.briefs.check()
                if self.repos is not None:
                    await self.repos.check()
                if self.usage is not None:
                    await self.usage.check_budget()
                if self.learning is not None:
                    await self.learning.check()
                if self.reports is not None:
                    await self.reports.check()
                if loop.time() - self._last_watch_check >= WATCH_CHECK_SECONDS:
                    self._last_watch_check = loop.time()
                    await self.check_watches()
                    await self.check_followups()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Never let one bad check stop the loop.
            await asyncio.sleep(MEETING_CHECK_SECONDS)

    # Meetings -----------------------------------------------------------------------------

    async def check_meetings(self) -> int:
        settings = self.store.settings()
        if not settings["meeting_prep"] or self.calendar is None:
            return 0
        now = self.clock()
        lead = timedelta(minutes=int(settings["lead_minutes"]))
        window = SimpleNamespace(
            start=now.isoformat(), end=(now + lead + timedelta(minutes=1)).isoformat()
        )
        try:
            events = (await self.calendar.events(window))["events"]
        except Exception:
            return 0  # No calendar access yet; nothing to announce.
        sent = 0
        for event in events:
            start = datetime.fromisoformat(event["start"])
            if event.get("all_day") or start <= now or start - now > lead:
                continue
            if not self.store.notify_once(f"meeting:{event.get('id')}:{event['start']}"):
                continue
            minutes = max(1, round((start - now).total_seconds() / 60))
            where = f" · {event['location']}" if event.get("location") else ""
            await self.notify(
                f"In {minutes} min: {event.get('title') or 'Meeting'}",
                f"{start.strftime('%H:%M')}–{datetime.fromisoformat(event['end']).strftime('%H:%M')}"
                f"{where} · {event.get('calendar', '')}".strip(" ·"),
            )
            sent += 1
        return sent

    # Watches ------------------------------------------------------------------------------

    async def _slack_identity(self) -> str | None:
        if self.accounts is None:
            return None
        try:
            accounts = (await self.accounts.list_accounts())["accounts"]
        except Exception:
            return None
        return next((item["identity"] for item in accounts if item["provider"] == "slack"), None)

    async def _matches(self, watch: Watch) -> list[tuple[str, str, str]]:
        """(stable id, title, detail) for current matches, newest first."""
        if watch.kind == "email":
            if self.gmail is None:
                raise ValueError("Gmail isn't connected.")
            found = await self.gmail.search(
                SimpleNamespace(account_id=None, query=f"{watch.query} newer_than:2d", limit=10)
            )
            return [
                (
                    item["message_id"],
                    f"Email from {item['from'].split('<')[0].strip() or item['from']}",
                    item["subject"] or "(no subject)",
                )
                for item in found["messages"]
            ]
        if self.slack is None:
            raise ValueError("Slack isn't connected.")
        query = slack_query(watch.query, await self._slack_identity())
        found = await self.slack.search(SimpleNamespace(account_id=None, query=query, limit=10))
        return [
            (
                f"{item.get('channel')}:{item.get('ts')}",
                f"Slack: {item.get('user') or 'someone'}"
                + (f" in #{item['channel']}" if item.get("channel") else ""),
                " ".join(str(item.get("text") or "").split())[:120],
            )
            for item in found["messages"]
        ]

    async def check_watches(self) -> int:
        sent = 0
        for watch in self.store.watches():
            if watch.expires_at and time.time() > watch.expires_at:
                self.store.expire(watch.id)
                await self.notify(
                    f"Stopped watching: {watch.label}",
                    "It didn't happen in time. Ask Bridge to watch it again if you still care.",
                    view="today",
                )
                continue
            if watch.kind in {"email", "slack"}:
                sent += await self._check_search(watch)
            else:
                sent += await self.check_condition(watch)
        return sent

    async def _check_search(self, watch: Watch) -> int:
        """Email and Slack: new matching messages (only the first one for "once")."""
        try:
            matches = await self._matches(watch)
        except Exception as exc:
            self.store.failed(
                watch.id, str(exc) if isinstance(exc, ValueError) else "Check failed."
            )
            return 0
        seen = set(watch.seen)
        fresh = [item for item in matches if item[0] not in seen]
        # The first check only records what's already there; no flood of old items.
        place = "inbox" if watch.kind == "email" else "today"
        sent = 0
        if watch.baseline_done and fresh and watch.once:
            _key, title, detail = fresh[0]
            await self.notify(f"✓ {watch.label}", f"{title}: {detail}", view=place)
            self.store.happened(watch.id, f"{title}: {detail}", done=True)
            sent = 1
        elif watch.baseline_done:
            for _key, title, detail in reversed(fresh[:3]):
                await self.notify(f"{title} · {watch.label}", detail, view=place)
                sent += 1
            if len(fresh) > 3:
                await self.notify(
                    f"{watch.label}", f"{len(fresh) - 3} more new matches.", view=place
                )
            if fresh:
                self.store.happened(watch.id, f"{fresh[0][1]}: {fresh[0][2]}", done=False)
        self.store.checked(watch.id, [*watch.seen, *(item[0] for item in reversed(fresh))])
        return sent

    async def check_condition(self, watch: Watch, *, quiet: bool = False) -> int:
        """GitHub, Jira and apps: look, compare with last time, tell you if it happened.
        With quiet=True (just created) nothing is sent; the caller reports it instead."""
        if self.watchers is None:
            return 0
        if (
            watch.kind == "app"
            and watch.last_checked
            and time.time() - watch.last_checked < APP_EVERY - 5
        ):
            return 0
        before = watch.state if watch.baseline_done else None
        try:
            now = await self.watchers.look(watch)
            event = await self.watchers.decide(watch, before, now)
        except WatchError as exc:
            self.store.failed(watch.id, str(exc))
            return 0
        except Exception:
            self.store.failed(watch.id, "Check failed. Bridge will try again.")
            return 0
        self.store.observed(watch.id, self.watchers.remember(watch, now))
        if event is None:
            return 0
        headline, detail = event[0], event[1]
        done = watch.once and (len(event) < 3 or event[2])
        self.store.happened(watch.id, f"{headline} — {detail}", done=done)
        if not quiet:
            await self.notify(headline, detail, view="today")
        return 1

    # Follow-ups ---------------------------------------------------------------------------

    async def check_followups(self) -> int:
        """Notify when the person replied, or once when the deadline passes without a reply."""
        if self.gmail is None:
            return 0
        now = self.clock()
        sent = 0
        for item in self.store.followups():
            if item.status not in {"waiting", "overdue"}:
                continue
            due = datetime.fromisoformat(item.due)
            if now - due > timedelta(days=7):
                self.store.set_followup(item.id, "expired")
                continue
            try:
                found = await self.gmail.search(
                    SimpleNamespace(
                        account_id=None, query=f"from:{item.email} after:{int(item.since)}", limit=3
                    )
                )
            except Exception:
                continue
            if found["messages"]:
                self.store.set_followup(item.id, "replied")
                subject = found["messages"][0]["subject"] or "(no subject)"
                await self.notify(f"✓ {item.name} replied", subject, view="inbox")
                sent += 1
            elif item.status == "waiting" and now >= due:
                self.store.set_followup(item.id, "overdue")
                await self.notify(
                    f"No reply from {item.name} yet",
                    f"About: {item.about}. Ask Bridge to “draft a follow-up to {item.name}”.",
                    view="inbox",
                )
                sent += 1
        return sent
