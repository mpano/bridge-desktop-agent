"""Where Bridge's AI requests go: OpenAI with your own key, or your Bridge plan.

With a Bridge plan, requests go to Bridge Cloud, which pays OpenAI from your plan's credit
(its answers look exactly like OpenAI's). Voice conversations still talk to OpenAI directly;
Bridge Cloud only opens them.
"""

from __future__ import annotations

OPENAI = "https://api.openai.com/v1"
NO_AI = "Add your OpenAI key, or start a Bridge plan, in Settings first."


def cloud(settings) -> bool:
    return getattr(settings, "ai_provider", "own_key") == "bridge"


def base(settings) -> str:
    if cloud(settings):
        return str(getattr(settings, "cloud_url", "")).rstrip("/") + "/v1/openai"
    return OPENAI


def key(settings) -> str:
    if cloud(settings):
        token = getattr(settings, "cloud_token", None)
        return token.get_secret_value() if token is not None else ""
    return settings.openai_api_key.get_secret_value()


def url(settings, path: str) -> str:
    return f"{base(settings)}/{path.lstrip('/')}"


def headers(settings) -> dict:
    return {"Authorization": f"Bearer {key(settings)}"}
