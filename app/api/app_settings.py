"""Routes for the Settings screen: shortcuts, voice, privacy, Mac permissions and restart.

Changes are written to .env (only the keys listed below) and apply after Bridge restarts,
which the screen offers as one button.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config.env_file import update_env
from app.tools.screen.context import BLOCKED_BY_DEFAULT

SCREEN_TOOL = "screen_context"
SHARED_RESULTS = [
    "email_search",
    "email_read_thread",
    "calendar_events",
    "calendar_free_time",
    "slack_search",
    "slack_read_channel",
]


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice_shortcut: bool | None = None
    text_actions_shortcut: bool | None = None
    dictation_shortcut: bool | None = None
    dictation_cleanup: bool | None = None
    dictation_pause_seconds: float | None = Field(default=None, ge=1, le=10)
    speech: Literal["local", "openai"] | None = None
    share_results: bool | None = None
    share_screen: bool | None = None
    blocked_apps: list[str] | None = Field(default=None, max_length=30)
    keep_chats_days: Literal[0, 7, 30] | None = None

    @field_validator("blocked_apps")
    @classmethod
    def app_names(cls, values):
        if values is None:
            return values
        cleaned = [" ".join(value.split()) for value in values if value.strip()]
        if any(len(value) > 80 or any(ch in value for ch in '"\\\n') for value in cleaned):
            raise ValueError("Use plain app names, like “Banking”.")
        return list(dict.fromkeys(cleaned))


def env_path() -> Path:
    return Path(".env").resolve()


def install(app: FastAPI, authorize, settings) -> None:
    app.state.restart_needed = False

    def allowlist() -> list[str]:
        return list(settings.remote_tool_result_allowlist)

    @app.get("/api/v1/settings/app", dependencies=[Depends(authorize)])
    async def read(request: Request):
        permissions = []
        if sys.platform == "darwin":
            from app.desktop.permissions import statuses

            permissions = await asyncio.to_thread(statuses)
        shared = allowlist()
        return {
            "shortcuts": {
                "voice": settings.voice_shortcut_enabled,
                "text_actions": settings.text_actions_shortcut_enabled,
                "dictation": settings.dictation_shortcut_enabled,
            },
            "voice": {
                "speech": settings.voice_stt_provider,
                "dictation_cleanup": settings.dictation_cleanup,
                "dictation_pause_seconds": settings.dictation_pause_seconds,
            },
            "privacy": {
                "results": settings.remote_tool_results,
                "shared_tools": shared,
                "share_results": settings.remote_tool_results == "all"
                or (
                    settings.remote_tool_results == "allowlist"
                    and any(t != SCREEN_TOOL for t in shared)
                ),
                "share_screen": settings.remote_tool_results == "all" or SCREEN_TOOL in shared,
                "blocked_apps": settings.screen_context_blocked_apps,
                "always_blocked": [
                    "1Password",
                    "Keychain Access",
                    "Passwords",
                    "Bitwarden",
                    "LastPass",
                    "Dashlane",
                    "System Settings",
                ],
                "keep_chats_days": settings.chat_retention_days,
                "model": settings.openai_model,
                "openai_key": bool(settings.openai_api_key.get_secret_value()),
            },
            "permissions": permissions,
            "restart_needed": request.app.state.restart_needed,
            "can_restart": bool(os.environ.get("BRIDGE_APP_BUNDLE")),
            "always_blocked_ids": len(BLOCKED_BY_DEFAULT),
        }

    @app.post("/api/v1/settings/app", dependencies=[Depends(authorize)])
    async def write(payload: AppSettings, request: Request):
        changes: dict[str, str] = {}
        flag = lambda value: "true" if value else "false"  # noqa: E731
        if payload.voice_shortcut is not None:
            changes["VOICE_SHORTCUT_ENABLED"] = flag(payload.voice_shortcut)
        if payload.text_actions_shortcut is not None:
            changes["TEXT_ACTIONS_SHORTCUT_ENABLED"] = flag(payload.text_actions_shortcut)
        if payload.dictation_shortcut is not None:
            changes["DICTATION_SHORTCUT_ENABLED"] = flag(payload.dictation_shortcut)
        if payload.dictation_cleanup is not None:
            changes["DICTATION_CLEANUP"] = flag(payload.dictation_cleanup)
        if payload.dictation_pause_seconds is not None:
            changes["DICTATION_PAUSE_SECONDS"] = f"{payload.dictation_pause_seconds:g}"
        if payload.speech is not None:
            changes["VOICE_STT_PROVIDER"] = payload.speech
        if payload.share_results is not None or payload.share_screen is not None:
            shared = allowlist()
            results = [t for t in shared if t != SCREEN_TOOL] or SHARED_RESULTS
            screen = SCREEN_TOOL in shared or settings.remote_tool_results == "all"
            if payload.share_results is not None:
                results = results if payload.share_results else []
            elif settings.remote_tool_results != "allowlist":
                results = []
            if payload.share_screen is not None:
                screen = payload.share_screen
            new_list = results + ([SCREEN_TOOL] if screen else [])
            changes["REMOTE_TOOL_RESULTS"] = "allowlist" if new_list else "status_only"
            changes["REMOTE_TOOL_RESULT_ALLOWLIST"] = json.dumps(new_list)
            settings.remote_tool_result_allowlist = new_list
            settings.remote_tool_results = changes["REMOTE_TOOL_RESULTS"]
        if payload.blocked_apps is not None:
            changes["SCREEN_CONTEXT_BLOCKED_APPS"] = json.dumps(payload.blocked_apps)
        # Keeping chats applies right away; everything else after a restart.
        live: dict[str, str] = {}
        if payload.keep_chats_days is not None:
            live["CHAT_RETENTION_DAYS"] = str(payload.keep_chats_days)
        if not changes and not live:
            return {"saved": [], "restart_needed": request.app.state.restart_needed}
        try:
            await asyncio.to_thread(update_env, env_path(), {**changes, **live})
        except OSError:
            raise HTTPException(500, "Couldn't save your settings file.") from None
        if live:
            settings.chat_retention_days = payload.keep_chats_days
            store = getattr(request.app.state.agent, "chats", None)
            if store is not None:
                await asyncio.to_thread(store.set_days, payload.keep_chats_days)
        if changes:
            request.app.state.restart_needed = True
        return {
            "saved": sorted({**changes, **live}),
            "restart_needed": request.app.state.restart_needed,
        }

    @app.post("/api/v1/notifications/test", dependencies=[Depends(authorize)])
    async def test_notification(request: Request):
        await request.app.state.agent.scheduler.notify(
            "Bridge", "Notifications work. Click this to open Today.", view="today"
        )
        return {"sent": True}

    @app.post("/api/v1/app/restart", dependencies=[Depends(authorize)])
    async def restart():
        bundle = os.environ.get("BRIDGE_APP_BUNDLE", "")
        if not bundle.endswith(".app") or not Path(bundle).is_dir():
            raise HTTPException(409, "Quit Bridge from the menu bar and open it again.")
        # Reopen once this process has fully exited, then quit through the normal shutdown.
        wait = f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.3; done"
        script = f"{wait}; /usr/bin/open {shlex.quote(bundle)}"
        subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True)
        asyncio.get_running_loop().call_later(0.4, os.kill, os.getpid(), signal.SIGTERM)
        return {"restarting": True}
