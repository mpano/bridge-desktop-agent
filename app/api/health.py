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

KEY_CHECK_SECONDS = 600
STALE_INBOX_HOURS = 24


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

    @app.get("/api/v1/health", dependencies=[Depends(authorize)])
    async def health(request: Request):
        agent = request.app.state.agent
        checks: list[dict] = []
        add = checks.append

        # Basics -----------------------------------------------------------------------------
        ok, detail = await openai_key()
        add(
            item(
                "Basics",
                "openai",
                "OpenAI key",
                "ok" if ok else "warn" if ok is None else "error",
                detail,
                None if ok else {"label": "Add a key", "view": "settings", "section": "privacy"},
            )
        )
        add(item("Basics", "service", "Bridge on this Mac", "ok", "Running."))

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
                    add(
                        item(
                            "Connections",
                            provider,
                            name,
                            "error",
                            problem(last),
                            {"label": "Reconnect", "view": "connections"},
                        )
                    )
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
                    add(
                        item(
                            "Connections",
                            provider,
                            name,
                            "error",
                            problem(last),
                            {"label": "Reconnect", "view": "connections"},
                        )
                    )
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
        return {"checks": checks, **summary, "checked_at": time.time()}
