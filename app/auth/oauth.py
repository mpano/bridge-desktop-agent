"""Sign in with Google or GitHub (OAuth 2 + PKCE) for the Bridge owner.

This only proves who is signing in. It requests no access to mail, repositories or
anything else, and no provider token is stored.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

PROVIDERS = {
    "google": {
        "name": "Google",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": "openid email profile",
    },
    "github": {
        "name": "GitHub",
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        "scope": "read:user user:email",
    },
}


class SignInError(RuntimeError):
    """Safe to show to the user."""


@dataclass
class PendingSignIn:
    provider: str
    intent: str  # "signup", "login" or "link"
    redirect_uri: str
    expires_at: float
    verifier: str = field(repr=False)
    session_id: str | None = None  # For "link": the signed-in session that asked.


@dataclass(frozen=True)
class Identity:
    provider: str
    subject: str
    email: str
    name: str
    login: str


class OAuthSignIn:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport
        self.pending: dict[str, PendingSignIn] = {}

    def credentials(self, provider: str) -> tuple[str, str]:
        if provider == "google":
            return (
                self.settings.google_client_id,
                self.settings.google_client_secret.get_secret_value(),
            )
        return (
            self.settings.github_client_id,
            self.settings.github_client_secret.get_secret_value(),
        )

    def configured(self, provider: str) -> bool:
        client_id, secret = self.credentials(provider)
        # GitHub OAuth Apps always need their secret for the code exchange.
        return bool(client_id) and (provider == "google" or bool(secret))

    def start(self, provider: str, intent: str, origin: str, session_id: str | None = None):
        if provider not in PROVIDERS or not self.configured(provider):
            raise SignInError(f"{provider.capitalize()} sign-in isn't set up in .env yet.")
        now = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if v.expires_at > now}
        if len(self.pending) >= 20:
            raise SignInError("Too many unfinished sign-ins. Wait a few minutes and try again.")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        redirect = f"{origin}/api/v1/auth/oauth/{provider}/callback"
        self.pending[state] = PendingSignIn(
            provider, intent, redirect, now + 600, verifier, session_id
        )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        spec = PROVIDERS[provider]
        params = {
            "client_id": self.credentials(provider)[0],
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": spec["scope"],
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if provider == "google":
            params["prompt"] = "select_account"
        return spec["authorize"] + "?" + urlencode(params)

    def take(self, provider: str, state: str) -> PendingSignIn:
        pending = self.pending.pop(state, None)  # Single use, even if the exchange fails.
        if (
            pending is None
            or pending.provider != provider
            or pending.expires_at <= time.monotonic()
        ):
            raise SignInError("This sign-in link expired or was already used. Try again.")
        return pending

    async def _call(self, client, method: str, url: str, **kwargs) -> dict:
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.HTTPError:
            raise SignInError(
                "Couldn't reach the sign-in provider. Check your connection."
            ) from None
        if response.status_code != 200 or len(response.content) > 1_000_000:
            raise SignInError("The sign-in provider rejected the request. Try again.")
        try:
            data = response.json()
        except ValueError:
            raise SignInError("The sign-in provider sent an unexpected answer.") from None
        if isinstance(data, dict) and data.get("error"):
            raise SignInError("The sign-in provider rejected the request. Try again.")
        return data

    async def finish(self, pending: PendingSignIn, code: str) -> Identity:
        client_id, secret = self.credentials(pending.provider)
        spec = PROVIDERS[pending.provider]
        form = {
            "client_id": client_id,
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": pending.verifier,
        }
        if secret:
            form["client_secret"] = secret
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=False, trust_env=False, transport=self.transport
        ) as client:
            tokens = await self._call(
                client, "POST", spec["token"], data=form, headers={"Accept": "application/json"}
            )
            access = tokens.get("access_token")
            if not isinstance(access, str) or not access:
                raise SignInError("The sign-in provider didn't confirm who you are.")
            auth = {"Authorization": f"Bearer {access}", "Accept": "application/json"}
            if pending.provider == "google":
                profile = await self._call(
                    client, "GET", "https://openidconnect.googleapis.com/v1/userinfo", headers=auth
                )
                if not profile.get("email_verified"):
                    raise SignInError("Use a Google account with a verified email address.")
                return Identity(
                    "google",
                    str(profile["sub"]),
                    profile["email"],
                    profile.get("name") or profile["email"],
                    profile["email"],
                )
            profile = await self._call(client, "GET", "https://api.github.com/user", headers=auth)
            emails = await self._call(
                client, "GET", "https://api.github.com/user/emails", headers=auth
            )
        primary = (
            next(
                (item["email"] for item in emails if item.get("primary") and item.get("verified")),
                None,
            )
            if isinstance(emails, list)
            else None
        )
        if not primary:
            raise SignInError("Add a verified primary email to your GitHub account first.")
        return Identity(
            "github",
            str(profile["id"]),
            primary,
            profile.get("name") or profile.get("login") or primary,
            profile.get("login") or primary,
        )
