"""Routes for first-launch setup: the OpenAI key, Mac permissions, apps, shortcuts, days."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.config.env_file import update_env
from app.llm import keychain

PermissionKey = Literal[
    "accessibility", "microphone", "calendars", "reminders", "contacts", "screen"
]


class KeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=20, max_length=300, pattern=r"^sk-[A-Za-z0-9_\-]+$")


class PermissionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: PermissionKey


class DoneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    done: bool = True


async def key_works(key: str) -> bool:
    """One cheap call to OpenAI itself; the key is sent nowhere else."""
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        response = await client.get(
            "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}
        )
    return response.status_code == 200


def install(app: FastAPI, authorize, settings) -> None:
    app.state.key_source = keychain.loaded_from or (
        "env" if settings.openai_api_key.get_secret_value() else ""
    )

    def use_key(request: Request, key: str) -> None:
        settings.openai_api_key = SecretStr(key)
        llm = getattr(request.app.state.agent.planner, "llm", None)
        if llm is not None and hasattr(llm, "_client"):
            llm._client = None  # The next request uses the new key.

    @app.get("/api/v1/onboarding", dependencies=[Depends(authorize)])
    async def state(request: Request):
        agent = request.app.state.agent
        permissions = []
        if sys.platform == "darwin":
            from app.desktop.permissions import statuses

            permissions = await asyncio.to_thread(statuses)
        connections = {"enabled": False, "providers": []}
        accounts = getattr(agent, "accounts", None)
        if accounts is not None:
            try:
                catalog = await accounts.catalog()
                connected = {item["provider"] for item in catalog.get("accounts", [])}
                connections = {
                    "enabled": catalog["enabled"],
                    "providers": [
                        {
                            "provider": p["provider"],
                            "name": p["name"],
                            "configured": p["configured"],
                            "connected": p["provider"] in connected,
                        }
                        for p in catalog["providers"]
                    ],
                }
            except Exception:
                pass
        days = agent.proactive_store.settings()
        return {
            "done": bool(days.get("onboarded")),
            "openai": {
                "set": bool(settings.openai_api_key.get_secret_value()),
                "source": request.app.state.key_source,
            },
            "permissions": permissions,
            "connections": connections,
            "usage": dict(request.app.state.usage),
            "days": {
                key: days[key]
                for key in (
                    "work_start",
                    "work_end",
                    "morning_plan",
                    "morning_time",
                    "meeting_prep",
                )
            },
        }

    @app.post("/api/v1/onboarding/openai-key", dependencies=[Depends(authorize)])
    async def save_key(payload: KeyInput, request: Request):
        try:
            works = await key_works(payload.key)
        except httpx.HTTPError:
            raise HTTPException(
                502, "Couldn't reach OpenAI to check the key. Check your connection."
            ) from None
        if not works:
            raise HTTPException(
                422, "OpenAI didn't accept that key. Copy it again from platform.openai.com."
            )
        try:
            await asyncio.to_thread(keychain.save_key, payload.key)
        except Exception:
            raise HTTPException(
                500, "Couldn't save the key in your Keychain. If macOS asked, choose Always Allow."
            ) from None
        use_key(request, payload.key)
        request.app.state.key_source = "keychain"
        return {"saved": True, "source": "keychain"}

    @app.post("/api/v1/onboarding/openai-key/move", dependencies=[Depends(authorize)])
    async def move_key(request: Request):
        """Move a key that's in .env into the Keychain, then blank it in .env."""
        key = settings.openai_api_key.get_secret_value()
        if not key:
            raise HTTPException(409, "There's no key in .env to move.")
        try:
            await asyncio.to_thread(keychain.save_key, key)
            await asyncio.to_thread(update_env, Path(".env").resolve(), {"OPENAI_API_KEY": ""})
        except Exception:
            raise HTTPException(
                500, "Couldn't move the key. It's still in .env, so nothing broke."
            ) from None
        request.app.state.key_source = "keychain"
        return {"moved": True, "source": "keychain"}

    @app.post("/api/v1/permissions/request", dependencies=[Depends(authorize)])
    async def ask_permission(payload: PermissionInput):
        if sys.platform != "darwin":
            raise HTTPException(409, "Permissions only apply on a Mac.")
        from app.desktop import permissions

        await asyncio.to_thread(permissions.request, payload.key)
        return {"requested": payload.key}

    @app.post("/api/v1/onboarding/done", dependencies=[Depends(authorize)])
    async def done(payload: DoneInput, request: Request):
        request.app.state.agent.proactive_store.update_settings(onboarded=payload.done)
        return {"done": payload.done}
