"""Routes for Settings › Usage: what Bridge spends on AI, which model does which job, budget."""

from __future__ import annotations

import asyncio
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.config.env_file import update_env
from app.llm.usage import CHOICES

MODEL = Field(default=None, pattern=r"^[a-z0-9.-]{3,40}$")


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    smart: str | None = MODEL
    fast: str | None = MODEL
    budget: float | None = Field(default=None, ge=0, le=100000)
    mode: Literal["warn", "save"] | None = None


def install(app: FastAPI, authorize, settings) -> None:
    def usage(request: Request):
        found = getattr(request.app.state.agent, "usage", None)
        if found is None:
            raise HTTPException(404, "Usage isn't available.")
        return found

    async def works(model: str) -> None:
        """A tiny test call, so a model your key can't use never breaks Ask."""
        key = settings.openai_api_key.get_secret_value()
        if not key:
            raise HTTPException(409, "Add your OpenAI key first.")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://api.openai.com/v1/responses",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": model,
                        "input": "Reply with OK.",
                        "max_output_tokens": 16,
                        "store": False,
                    },
                )
        except httpx.HTTPError:
            raise HTTPException(502, "Couldn't reach OpenAI to check the model.") from None
        if response.status_code >= 400:
            raise HTTPException(
                409, f"{model} didn't work with your OpenAI key, so it wasn't saved."
            )

    @app.get("/api/v1/usage", dependencies=[Depends(authorize)])
    async def summary(request: Request):
        return usage(request).summary()

    @app.post("/api/v1/usage/settings", dependencies=[Depends(authorize)])
    async def change(payload: Change, request: Request):
        changes: dict[str, str] = {}
        for value, field, key in (
            (payload.smart, "model_smart", "MODEL_SMART"),
            (payload.fast, "model_fast", "MODEL_FAST"),
        ):
            if value is None or value == getattr(settings, field):
                continue
            if value not in CHOICES and value != settings.openai_model:
                raise HTTPException(422, "Choose one of the listed models.")
            await works(value)
            changes[key] = value
            setattr(settings, field, value)
        if payload.budget is not None:
            changes["MONTHLY_BUDGET"] = f"{payload.budget:g}"
            settings.monthly_budget = payload.budget
        if payload.mode is not None:
            changes["BUDGET_MODE"] = payload.mode
            settings.budget_mode = payload.mode
        if changes:
            from app.api.app_settings import env_path

            await asyncio.to_thread(update_env, env_path(), changes)
        return usage(request).summary()
