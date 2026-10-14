"""Settings › Health: is each part of Bridge working right now, and the one tap that fixes it.

Every check reads Bridge's own state (connections, last runs, permissions); the only request
that leaves the Mac is a check that the OpenAI key still works, at most every ten minutes.
States: "ok", "warn" (works, but look), "error" (broken), "off" (an optional feature you
haven't turned on).
"""

from __future__ import annotations

import asyncio
import sys
import time

import httpx
from fastapi import Depends, FastAPI, Request

from app.llm import endpoint

KEY_CHECK_SECONDS = 600
STALE_INBOX_HOURS = 24
WATCH_SECONDS = 600


def item(area: str, key: str, name: str, state: str, detail: str, fix: dict | None = None):
    return {"area": area, "id": key, "name": name, "state": state, "detail": detail, "fix": fix}


def problem(last: dict) -> str:
    """The latest failure for a service, in words."""
    return f"{last.get('event')} {ago(last.get('at', 0))}: {last.get('detail', '')}"


def ago(seconds: float) -> str:
    minutes = int((time.time() - seconds) / 60)
    if minutes < 2:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


LOGIN = (
    "expired",
    "invalid_auth",
    "token_revoked",
    "not_authed",
    "invalid_grant",
    "401",
    "unauthorized",
    "account_inactive",
    "reconnect",
)
PERMISSION = ("permission", "missing_scope", "forbidden", "403", "not_allowed")
SLOW_DOWN = ("429", "rate limit", "ratelimited", "too many requests")
OFFLINE = ("timed out", "timeout", "couldn't reach", "network", "connection error", "503", "502")


def failing(provider: str, name: str, last: dict) -> dict:
    """A service's latest failure in plain words, and what to do about it."""
    text = str(last.get("detail") or "").lower()
    when = ago(last.get("at", 0))
    reconnect = {"label": "Reconnect", "view": "connections"}
    if any(word in text for word in LOGIN):
        return item(
            "Connections",
            provider,
            name,
            "error",
            f"Login expired ({when}). Reconnect to keep it working.",
            reconnect,
        )
    if any(word in text for word in PERMISSION):
        return item(
            "Connections",
            provider,
            name,
            "error",
            f"Bridge needs more permission ({when}). Reconnect and allow it.",
            reconnect,
        )
    if any(word in text for word in SLOW_DOWN):
        return item(
            "Connections",
            provider,
            name,
            "warn",
            f"{name} asked Bridge to slow down ({when}). It tries again by itself.",
        )
    if any(word in text for word in OFFLINE):
        return item(
            "Connections",
            provider,
            name,
            "warn",
            f"Couldn't reach {name} ({when}). Bridge tries again by itself.",
        )
    return item("Connections", provider, name, "error", problem(last), reconnect)


def money(dollars: float) -> str:
    return "$0" if dollars < 0.005 else f"${dollars:,.2f}"


def spending(week: dict) -> dict:
    """AI spending this week, and a heads-up when it jumps or the budget is near."""
    usage = {"label": "Usage", "view": "settings", "section": "usage"}
    detail = f"{money(week['spent'])} in the last 7 days"
    detail += f" (the week before: {money(week['previous'])})." if week["previous"] else "."
    if week["budget"] and week["month"] >= week["budget"]:
        return item(
            "Basics",
            "spending",
            "AI spending",
            "warn",
            f"Over this month's {money(week['budget'])} budget. {detail}",
            usage,
        )
    if week["budget"] and week["month"] >= 0.8 * week["budget"]:
        return item(
            "Basics",
            "spending",
            "AI spending",
            "warn",
            f"{money(week['month'])} of this month's {money(week['budget'])} budget. " + detail,
            usage,
        )
    if week["spent"] >= 1 and week["previous"] and week["spent"] > 2 * week["previous"]:
        top = week["top"][0]["label"] if week["top"] else "Bridge"
        return item(
            "Basics",
            "spending",
            "AI spending",
            "warn",
            f"More than double the week before, mostly {top}. {detail}",
            usage,
        )
    return item("Basics", "spending", "AI spending", "ok", detail)


LOW_CREDIT = 0.25
ADD_KEY = {"label": "Add a key", "view": "settings", "section": "privacy"}


async def plan_check(cloud) -> dict:
    """With a Bridge plan: is there AI credit left?"""
    from app.integrations.cloud import CloudError

    fix = {"label": "Plan", "view": "settings", "section": "plan"}
    try:
        account = await cloud.overview()
    except CloudError as exc:
        return item("Basics", "openai", "Bridge plan", "error", str(exc), fix)
    if account is None:
        return item("Basics", "openai", "Bridge plan", "error", "This Mac isn't signed in.", fix)
    left = float(account.get("balance") or 0)
    name = "Bridge Plus" if account.get("plan") == "plus" else "Free trial"
    if left <= 0:
        return item(
            "Basics",
            "openai",
            "Bridge plan",
            "error",
            f"{name}: the AI credit has run out. Add credit to keep using Bridge.",
            fix,
        )
    if left < LOW_CREDIT:
        return item(
            "Basics",
            "openai",
            "Bridge plan",
            "warn",
            f"{name}: {money(left)} of AI credit left.",
            fix,
        )
    return item(
        "Basics", "openai", "Bridge plan", "ok", f"{name}: {money(left)} of AI credit left."
    )


class Alerts:
    """Tell you once when something breaks (and again only if it breaks again later)."""

    WATCHED = {"Connections"}

    def __init__(self):
        self.told: set[str] = set()

    def news(self, checks: list[dict]) -> list[dict]:
        broken = {
            c["id"]: c
            for c in checks
            if c["state"] == "error"
            and (
                c["area"] in self.WATCHED
                or (c["id"] == "openai" and "No OpenAI" not in c["detail"])
            )
        }
        fresh = [c for key, c in broken.items() if key not in self.told]
        self.told = set(broken)
        return fresh


async def watch(collect, agent, alerts: Alerts | None = None, first: float = 90) -> None:
    """While Bridge runs: look every 10 minutes, and say when a connection breaks."""
    alerts = alerts or Alerts()
    await asyncio.sleep(first)
    while True:
        try:
            notify = getattr(agent, "notify", None)
            for check in alerts.news((await collect(agent))["checks"]):
                if notify is not None:
                    await notify(f"{check['name']} needs you", check["detail"], "health")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(WATCH_SECONDS)


def install(app: FastAPI, authorize, settings) -> None:
    key_check: dict = {"at": 0.0, "ok": None, "detail": ""}

    async def openai_key() -> tuple[bool | None, str]:
        secret = settings.openai_api_key.get_secret_value()
        if not secret:
            return False, "No OpenAI key yet."
        if time.time() - key_check["at"] < KEY_CHECK_SECONDS and key_check["ok"] is not None:
            return key_check["ok"], key_check["detail"]
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {secret}"},
                )
            ok = response.status_code == 200
            detail = (
                f"Working · {settings.openai_model}"
                if ok
                else "OpenAI refused the key. Add a new one."
                if response.status_code == 401
                else f"OpenAI answered {response.status_code}. Try again soon."
            )
        except httpx.HTTPError:
            ok, detail = None, "Couldn't reach OpenAI to check. Are you online?"
        key_check.update(at=time.time(), ok=ok, detail=detail)
        return ok, detail

    async def collect(agent) -> dict:
        checks: list[dict] = []
        add = checks.append

        # Basics -----------------------------------------------------------------------------
        if endpoint.cloud(settings) and getattr(agent, "cloud", None) is not None:
            add(await plan_check(agent.cloud))
        else:
            ok, detail = await openai_key()
            add(
                item(
                    "Basics",
                    "openai",
                    "OpenAI key",
                    "ok" if ok else "warn" if ok is None else "error",
                    detail,
                    None
                    if ok
                    else {"label": "Add a key", "view": "settings", "section": "privacy"},
                )
            )
        add(item("Basics", "service", "Bridge on this Mac", "ok", "Running."))
        usage = getattr(agent, "usage", None)
        week = None
        if usage is not None and hasattr(usage, "week"):
            week = usage.week()
            add(spending(week))

        # Mac permissions -----------------------------------------------------------------
        if sys.platform == "darwin":
            from app.desktop.permissions import statuses

            for p in await asyncio.to_thread(statuses):
                state = {"allowed": "ok", "denied": "error"}.get(p["state"], "warn")
                text = {"allowed": "Allowed.", "denied": "Turned off for Bridge."}.get(
                    p["state"], "Not asked yet."
                )
                add(
                    item(
                        "Mac permissions",
                        p["key"],
                        p["name"],
                        state,
                        f"{text} {p['why']}",
                        None
                        if state == "ok"
                        else {"label": "Open Settings", "url": p["settings_url"]},
                    )
                )

        # Connections ---------------------------------------------------------------------
        accounts = getattr(agent, "accounts", None)
        connected, activity = set(), []
        if accounts is not None:
            try:
                catalog = await accounts.catalog()
                connected = {a["provider"] for a in catalog.get("accounts", [])}
                activity = catalog.get("activity", [])
                enabled = catalog.get("enabled", False)
            except Exception:
                enabled = False
            names = {"gmail": "Gmail", "google_calendar": "Google Calendar", "slack": "Slack"}
            for provider, name in names.items():
                last = next((a for a in activity if a.get("provider") == provider), None)
                if not enabled:
                    add(
                        item(
                            "Connections",
                            provider,
                            name,
                            "off",
                            "Connected services are off.",
                            {"label": "Connections", "view": "connections"},
                        )
                    )
                elif provider not in connected:
                    add(
                        item(
                            "Connections",
                            provider,
                            name,
                            "off",
                            "Not connected.",
                            {"label": "Connect", "view": "connections"},
                        )
                    )
                elif last and not last.get("ok", True):
                    add(failing(provider, name, last))
                else:
                    add(
                        item(
                            "Connections",
                            provider,
                            name,
                            "ok",
                            f"Connected{' · last used ' + ago(last['at']) if last else ''}.",
                        )
                    )
        tokens = getattr(agent, "tokens", None)
        if tokens is not None:
            try:
                work = await tokens.accounts()
            except Exception:
                work = {}
            for provider, name in (("jira", "Jira & Confluence"), ("github", "GitHub")):
                last = next((a for a in activity if a.get("provider") == provider), None)
                if provider not in work:
                    add(
                        item(
                            "Connections",
                            provider,
                            name,
                            "off",
                            "Not connected.",
                            {"label": "Connect", "view": "connections"},
                        )
                    )
                elif last and not last.get("ok", True):
                    add(failing(provider, name, last))
                else:
                    add(
                        item(
                            "Connections",
                            provider,
                            name,
                            "ok",
                            f"Connected as {work[provider].get('identity', '')}.",
                        )
                    )

        mcp = getattr(agent, "mcp", None)
        if mcp is not None:
            for server in mcp.servers():
                state = mcp.status.get(server["id"], {}).get("state", "unknown")
                detail = mcp.status.get(server["id"], {}).get("detail", "")
                add(
                    item(
                        "Connections",
                        f"mcp-{server['id']}",
                        server["name"],
                        {"ok": "ok", "sign_in": "error", "error": "error"}.get(state, "ok"),
                        detail if state != "unknown" else "Connects when Bridge first uses it.",
                        None
                        if state in {"ok", "unknown"}
                        else {"label": "Connections", "view": "connections"},
                    )
                )

        # The assistant ------------------------------------------------------------------
        sorted_at = [
            source.last[0]
            for source in (getattr(agent, "inbox", None), getattr(agent, "slack_inbox", None))
            if source is not None and getattr(source, "last", None)
        ]
        if {"gmail", "slack"} & connected:
            if not sorted_at:
                add(
                    item(
                        "Assistant",
                        "inbox",
                        "Inbox sorting",
                        "warn",
                        "Not sorted since Bridge started.",
                        {"label": "Sort now", "action": "/api/v1/inbox/triage"},
                    )
                )
            elif time.time() - max(sorted_at) > STALE_INBOX_HOURS * 3600:
                add(
                    item(
                        "Assistant",
                        "inbox",
                        "Inbox sorting",
                        "warn",
                        f"Last sorted {ago(max(sorted_at))}.",
                        {"label": "Sort now", "action": "/api/v1/inbox/triage"},
                    )
                )
            else:
                add(
                    item(
                        "Assistant",
                        "inbox",
                        "Inbox sorting",
                        "ok",
                        f"Sorted {ago(max(sorted_at))}.",
                    )
                )
        commitments = getattr(agent, "commitments", None)
        if commitments is not None and commitments.connected:
            scan = commitments.status()
            if scan["error"]:
                add(
                    item(
                        "Assistant",
                        "promises",
                        "Promises",
                        "error",
                        scan["error"],
                        {"label": "Check now", "action": "/api/v1/commitments/scan"},
                    )
                )
            elif scan["running"]:
                add(item("Assistant", "promises", "Promises", "ok", "Reading your messages now…"))
            elif not scan["last_scan"]:
                add(
                    item(
                        "Assistant",
                        "promises",
                        "Promises",
                        "warn",
                        "Not checked yet.",
                        {"label": "Check now", "action": "/api/v1/commitments/scan"},
                    )
                )
            else:
                add(
                    item(
                        "Assistant",
                        "promises",
                        "Promises",
                        "ok",
                        f"Checked {ago(scan['last_scan'])}.",
                    )
                )
        store = getattr(agent, "proactive_store", None)
        if store is not None:
            s = store.settings()
            routines = [
                name
                for name, on in (
                    ("morning brief", s.get("morning_plan")),
                    ("evening wrap-up", s.get("evening_summary")),
                )
                if on
            ]
            add(
                item(
                    "Assistant",
                    "briefs",
                    "Morning brief & wrap-up",
                    "ok" if routines else "off",
                    ("On: " + " and ".join(routines) + ".") if routines else "Both are off.",
                    None if routines else {"label": "Automations", "view": "automations"},
                )
            )
        chats = getattr(agent, "chats", None)
        if chats is not None:
            add(
                item(
                    "Assistant",
                    "chats",
                    "Saved chats",
                    "ok" if chats.days else "off",
                    f"Kept for {chats.days} days." if chats.days else "Chats aren't kept.",
                    None
                    if chats.days
                    else {"label": "Privacy", "view": "settings", "section": "privacy"},
                )
            )

        # Phone, voice and using the Mac --------------------------------------------------
        phone = getattr(agent, "phone", None)
        if phone is not None:
            if not phone.enabled:
                add(
                    item(
                        "Phone & voice",
                        "phone",
                        "Bridge on your phone",
                        "off",
                        "Not set up.",
                        {"label": "Set up", "view": "settings", "section": "phone"},
                    )
                )
            else:
                pushes = len(phone.subscriptions())
                add(
                    item(
                        "Phone & voice",
                        "phone",
                        "Bridge on your phone",
                        "ok" if pushes else "warn",
                        f"On at {phone.host}."
                        + ("" if pushes else " Turn on notifications in the phone's You tab."),
                        None
                        if pushes
                        else {"label": "Phone settings", "view": "settings", "section": "phone"},
                    )
                )
        talk = getattr(agent, "talk", None)
        if talk is not None:
            add(
                item(
                    "Phone & voice",
                    "talk",
                    "Conversation mode",
                    "ok" if talk.enabled else "off",
                    "On." if talk.enabled else "Off.",
                    None
                    if talk.enabled
                    else {"label": "Voice settings", "view": "settings", "section": "voice"},
                )
            )
        operator = getattr(agent, "operator", None)
        if operator is not None:
            if not operator.enabled:
                add(
                    item(
                        "Phone & voice",
                        "operator",
                        "Use your Mac",
                        "off",
                        "Off.",
                        {"label": "Turn on", "view": "settings", "section": "operator"},
                    )
                )
            else:
                last = operator.recent()[:1]
                failed = last and last[0]["status"] == "failed"
                add(
                    item(
                        "Phone & voice",
                        "operator",
                        "Use your Mac",
                        "warn" if failed else "ok",
                        f"Last task didn't finish: {last[0]['result']}" if failed else "On.",
                        None,
                    )
                )

        # Automations ----------------------------------------------------------------------
        automations = {"label": "Automations", "view": "automations"}
        controller = getattr(agent, "proactive_controller", None)
        recipes = getattr(agent, "recipes", None)
        if controller is not None and store is not None:
            owned = recipes.watch_ids() if recipes is not None else set()
            watches = [w for w in store.watches() if w.id not in owned]
            broken = [w for w in watches if w.last_error]
            if not watches:
                add(
                    item(
                        "Automations",
                        "watchers",
                        "Watchers",
                        "off",
                        "Not watching anything. Try “tell me when PR 412 is merged”.",
                        automations,
                    )
                )
            elif broken:
                add(
                    item(
                        "Automations",
                        "watchers",
                        "Watchers",
                        "warn",
                        f"“{broken[0].label}” can't be checked: {broken[0].last_error}",
                        automations,
                    )
                )
            else:
                add(
                    item(
                        "Automations",
                        "watchers",
                        "Watchers",
                        "ok",
                        f"Watching {len(watches)} thing{'s' if len(watches) > 1 else ''}.",
                    )
                )
        if recipes is not None:
            listed = recipes.overview()
            failed = [
                r
                for r in listed["recent"]
                if r["status"] == "failed" and time.time() - (r["decided"] or 0) < 86400
            ]
            problems = [r for r in listed["recipes"] if r["problem"]]
            if not listed["recipes"]:
                add(
                    item("Automations", "recipes", "Recipes", "off", "No recipes yet.", automations)
                )
            elif problems:
                add(
                    item(
                        "Automations",
                        "recipes",
                        "Recipes",
                        "warn",
                        f"“{problems[0]['name']}”: {problems[0]['problem']}",
                        automations,
                    )
                )
            elif failed:
                add(
                    item(
                        "Automations",
                        "recipes",
                        "Recipes",
                        "warn",
                        f"“{failed[0]['recipe']}” didn't finish: {failed[0]['result']}",
                        automations,
                    )
                )
            elif listed["waiting"]:
                add(
                    item(
                        "Automations",
                        "recipes",
                        "Recipes",
                        "warn",
                        f"{len(listed['waiting'])} waiting for your OK.",
                        {"label": "Today", "view": "today"},
                    )
                )
            else:
                n = len(listed["recipes"])
                add(
                    item(
                        "Automations",
                        "recipes",
                        "Recipes",
                        "ok",
                        f"{n} recipe{'s' if n > 1 else ''}, all fine.",
                    )
                )
        if store is not None and getattr(agent, "reports", None) is not None:
            s = store.settings()
            on = s.get("weekly_report")
            add(
                item(
                    "Automations",
                    "report",
                    "Weekly report",
                    "ok" if on else "off",
                    f"Drafted Fridays at {s.get('weekly_report_time', '16:00')}." if on else "Off.",
                    None if on else automations,
                )
            )

        # On your Mac ------------------------------------------------------------------------
        guides = getattr(agent, "guides", None)
        if guides is not None and operator is not None:
            if not operator.enabled:
                add(
                    item(
                        "On your Mac",
                        "guide",
                        "Show me how",
                        "off",
                        "Needs “Use your Mac” (Bridge has to see the screen).",
                        {"label": "Turn on", "view": "settings", "section": "operator"},
                    )
                )
            else:
                last = guides.recent()[:1]
                failed = last and last[0]["status"] == "failed"
                add(
                    item(
                        "On your Mac",
                        "guide",
                        "Show me how",
                        "warn" if failed else "ok",
                        "The last guide stopped early. Try again."
                        if failed
                        else "Ready: “show me how to …”.",
                    )
                )
        if getattr(agent, "explainer", None) is not None:
            from app.assistant.explain import screen_shared

            if not getattr(settings, "explain_shortcut_enabled", True):
                add(
                    item(
                        "On your Mac",
                        "explain",
                        "What's this? (⌃⌥W)",
                        "off",
                        "Shortcut off.",
                        {"label": "Shortcuts", "view": "settings", "section": "shortcuts"},
                    )
                )
            elif not screen_shared(settings):
                add(
                    item(
                        "On your Mac",
                        "explain",
                        "What's this? (⌃⌥W)",
                        "warn",
                        "Needs your OK to show OpenAI what's on screen.",
                        {"label": "Privacy", "view": "settings", "section": "privacy"},
                    )
                )
            else:
                add(
                    item(
                        "On your Mac",
                        "explain",
                        "What's this? (⌃⌥W)",
                        "ok",
                        "Point at anything and press ⌃⌥W.",
                    )
                )
        meetings = getattr(agent, "meetings", None)
        if meetings is not None:
            voice = {"label": "Voice settings", "view": "settings", "section": "voice"}
            last = meetings.store.recent(1)
            if not meetings.enabled:
                add(item("On your Mac", "meetings", "Meeting notes", "off", "Off.", voice))
            elif meetings.audio_factory is None or meetings.stt is None:
                add(
                    item(
                        "On your Mac",
                        "meetings",
                        "Meeting notes",
                        "warn",
                        "On, but this Mac can't listen (needs the Bridge app and speech to text).",
                        voice,
                    )
                )
            elif last and (last[0].get("summary") or {}).get("failed"):
                add(
                    item(
                        "On your Mac",
                        "meetings",
                        "Meeting notes",
                        "warn",
                        f"The summary of “{last[0]['title']}” couldn't be written.",
                        {"label": "Meetings", "view": "meetings"},
                    )
                )
            else:
                add(
                    item(
                        "On your Mac",
                        "meetings",
                        "Meeting notes",
                        "ok",
                        "Recording now." if meetings.current else "Ready: “take notes”.",
                    )
                )
        moments = getattr(agent, "moments", None)
        if moments is not None:
            waiting = len(moments.store.all(active_only=True))
            if moments.sense is None:
                state, detail = "off", "Needs the Bridge app (it notices which app is in front)."
            elif waiting:
                state = "ok"
                detail = f"Waiting for {waiting} moment{'s' if waiting > 1 else ''}."
            else:
                state, detail = "ok", "Ready: “remind me … when I open Figma”."
            add(item("On your Mac", "moments", "Reminders for a moment", state, detail))
        if getattr(agent, "buddy", None) is not None:
            on = bool(getattr(settings, "desk_buddy", True))
            add(
                item(
                    "On your Mac",
                    "desk",
                    "Bridge on your desk",
                    "ok" if on else "off",
                    "In the corner of your screen. Drag it anywhere." if on else "Hidden.",
                    None
                    if on
                    else {"label": "Shortcuts", "view": "settings", "section": "shortcuts"},
                )
            )

        summary = {
            "errors": sum(c["state"] == "error" for c in checks),
            "warnings": sum(c["state"] == "warn" for c in checks),
        }
        return {"checks": checks, **summary, "week": week, "checked_at": time.time()}

    @app.get("/api/v1/health", dependencies=[Depends(authorize)])
    async def health(request: Request):
        return await collect(request.app.state.agent)

    app.state.health_watch = lambda agent: watch(collect, agent)
