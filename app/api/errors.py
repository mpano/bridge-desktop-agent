"""What an error says on screen: the message, if it was written for people; otherwise a
plain fallback. Internal errors (a missing attribute, a broken connection object) never reach
the page as raw text."""

from __future__ import annotations

from app.integrations.models import IntegrationError

SAFE = (ValueError, IntegrationError, TimeoutError)


def friendly(exc: BaseException, fallback: str) -> str:
    if isinstance(exc, SAFE) and str(exc).strip():
        return str(exc)
    # Bridge's own user-facing errors are named "...Error" in app.assistant (MeetingError…).
    module = type(exc).__module__ or ""
    if module.startswith("app.") and type(exc).__name__.endswith("Error") and str(exc).strip():
        return str(exc)
    return fallback
