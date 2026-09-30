from datetime import datetime
from urllib.parse import quote
from uuid import uuid4

from app.integrations.models import IntegrationError
from app.integrations.providers import GOOGLE
from app.tools.productivity.timeslots import free_slots


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GoogleCalendarService:
    def __init__(self, accounts):
        self.accounts = accounts

    async def calendars(self, args):
        data = await self.accounts.request(
            args.account_id,
            "google_calendar",
            "GET",
            "users/me/calendarList",
            scopes=(GOOGLE + "calendar.calendarlist.readonly",),
            params={"maxResults": 100},
        )
        return {
            "calendars": [
                {key: item.get(key) for key in ("id", "summary", "timeZone", "accessRole")}
                for item in data.get("items", [])
            ],
            "has_more": bool(data.get("nextPageToken")),
        }

    async def events(self, args):
        data = await self.accounts.request(
            args.account_id,
            "google_calendar",
            "GET",
            "calendars/" + quote(args.calendar_id, safe="") + "/events",
            scopes=(GOOGLE + "calendar.events.readonly",),
            params={
                "timeMin": args.start,
                "timeMax": args.end,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 50,
            },
        )
        keys = (
            "id",
            "summary",
            "description",
            "location",
            "start",
            "end",
            "status",
            "transparency",
            "attendees",
            "htmlLink",
        )
        return {
            "events": [{key: item.get(key) for key in keys} for item in data.get("items", [])],
            "has_more": bool(data.get("nextPageToken")),
            "content_is_untrusted": True,
        }

    async def create(self, args):
        payload = {
            "id": uuid4().hex,
            "summary": args.title,
            "description": args.description,
            "start": {"dateTime": args.start},
            "end": {"dateTime": args.end},
            "attendees": [{"email": value} for value in args.attendees],
        }
        data = await self.accounts.request(
            args.account_id,
            "google_calendar",
            "POST",
            "calendars/" + quote(args.calendar_id, safe="") + "/events",
            scopes=(GOOGLE + "calendar.events",),
            params={"sendUpdates": args.send_updates},
            json=payload,
        )
        if not data.get("id"):
            raise IntegrationError(
                "Calendar returned no event ID. Check the calendar before retrying; "
                "the outcome is uncertain."
            )
        return {
            "event_id": data["id"],
            "url": data.get("htmlLink"),
            "title": args.title,
            "start": args.start,
            "end": args.end,
            "message": "Google Calendar created the event.",
        }

    async def free_slots(self, args):
        """Gaps of at least min_minutes between non-cancelled, busy events in the window."""
        listed = await self.events(args)
        window_start, window_end = parse(args.start), parse(args.end)
        busy = []
        for event in listed["events"]:
            if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
                continue
            start, end = event.get("start") or {}, event.get("end") or {}
            if "dateTime" in start and "dateTime" in end:
                busy.append((parse(start["dateTime"]), parse(end["dateTime"])))
            elif "date" in start and "date" in end:
                # All-day events block the day in the window's own timezone.
                zone = window_start.tzinfo
                busy.append(
                    (
                        datetime.fromisoformat(start["date"]).replace(tzinfo=zone),
                        datetime.fromisoformat(end["date"]).replace(tzinfo=zone),
                    )
                )
        slots = free_slots(busy, window_start, window_end, args.min_minutes)
        return {
            "free": [{"start": start.isoformat(), "end": end.isoformat()} for start, end in slots],
            "complete": not listed["has_more"],
        }

    async def delete(self, args):
        """Delete the one event in the window whose title matches; never guess among several."""
        wanted = args.title.strip().casefold()
        listed = await self.events(args)
        matches = [
            event
            for event in listed["events"]
            if (event.get("summary") or "").strip().casefold() == wanted
            and event.get("status") != "cancelled"
        ]
        if not matches:
            raise IntegrationError(f"No event titled “{args.title}” was found in that time window.")
        if len(matches) > 1:
            raise IntegrationError(
                f"{len(matches)} events titled “{args.title}” are in that window. Narrow the time."
            )
        await self.accounts.request(
            args.account_id,
            "google_calendar",
            "DELETE",
            "calendars/"
            + quote(args.calendar_id, safe="")
            + "/events/"
            + quote(matches[0]["id"], safe=""),
            scopes=(GOOGLE + "calendar.events",),
            params={"sendUpdates": args.send_updates},
        )
        return {
            "title": matches[0].get("summary"),
            "start": matches[0].get("start"),
            "message": "Google Calendar deleted the event.",
        }
