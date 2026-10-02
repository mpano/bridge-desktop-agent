"""The proactive assistant: meeting heads-ups and "tell me when …" watches.

Runs while Bridge's local service is open. It only reads (calendar, mail search, Slack
search) and posts local notifications — no model call, nothing is sent anywhere, and it
never acts on what it finds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from types import SimpleNamespace

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
        self._last_watch_check = 0.0

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                if self.focus is not None:
                    await self.focus.check()  # Ends a focus session whose time passed.
                await self.check_meetings()
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
            try:
                matches = await self._matches(watch)
            except Exception as exc:
                self.store.failed(
                    watch.id, str(exc) if isinstance(exc, ValueError) else "Check failed."
                )
                continue
            seen = set(watch.seen)
            fresh = [item for item in matches if item[0] not in seen]
            # The first check only records what's already there; no flood of old items.
            place = "inbox" if watch.kind == "email" else "today"
            if watch.baseline_done:
                for _key, title, detail in reversed(fresh[:3]):
                    await self.notify(f"{title} · {watch.label}", detail, view=place)
                    sent += 1
                if len(fresh) > 3:
                    await self.notify(
                        f"{watch.label}", f"{len(fresh) - 3} more new matches.", view=place
                    )
            self.store.checked(watch.id, [*watch.seen, *(item[0] for item in reversed(fresh))])
        return sent

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
