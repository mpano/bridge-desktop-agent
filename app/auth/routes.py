"""Dashboard sign-in: owner sign-up, password / Google / GitHub / passkey login, sessions.

Security model:
- Only the Mac's user can create the owner account: sign-up needs a "setup" session,
  which only the Bridge menu bar can create (single-use launch ticket), or the API token.
- Sessions are HttpOnly, SameSite=Strict cookies; the token itself is never stored.
- Every state-changing request must come from the dashboard's own origin.
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth.oauth import PROVIDERS, OAuthSignIn, SignInError
from app.auth.passkeys import PasskeyError, Passkeys
from app.auth.store import AuthStore

COOKIE = "bridge_session"
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")
MAX_FAILURES, LOCK_SECONDS = 5, 300


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SignUp(Body):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(max_length=254)
    password: str = Field(min_length=10, max_length=200)


class Login(Body):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=200)
    remember: bool = False


class Start(Body):
    intent: str = Field(pattern="^(signup|login|link)$")
    remember: bool = False


class PasskeyRegistration(Body):
    credential: dict
    name: str = Field(default="Touch ID on this Mac", min_length=1, max_length=60)


class PasskeyLogin(Body):
    credential: dict
    remember: bool = False


class Profile(Body):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(max_length=254)


class PasswordChange(Body):
    current: str = Field(default="", max_length=200)
    new: str = Field(min_length=10, max_length=200)


class Target(Body):
    value: str = Field(min_length=1, max_length=200)


def checked_email(value: str) -> str:
    value = value.strip()
    if not EMAIL.fullmatch(value):
        raise HTTPException(422, "Enter a valid email address.")
    return value


class AuthService:
    def __init__(self, settings):
        self.settings = settings
        self._store: AuthStore | None = None
        self.oauth = OAuthSignIn(settings)
        self._passkeys: Passkeys | None = None
        self.failures: list[float] = []

    @property
    def store(self) -> AuthStore:
        if self._store is None:
            self._store = AuthStore(self.settings.database_path)
        return self._store

    @property
    def passkeys(self) -> Passkeys:
        if self._passkeys is None:
            self._passkeys = Passkeys(self.store)
        return self._passkeys

    def session(self, request: Request):
        return self.store.session(request.cookies.get(COOKIE))

    def owner_session(self, request: Request):
        current = self.session(request)
        return current if current and current.kind == "owner" else None

    def sign_in(self, response, request: Request, *, remember: bool = False, kind="owner"):
        hours = (
            self.settings.auth_remember_days * 24
            if remember
            else (1 if kind == "setup" else self.settings.auth_session_hours)
        )
        token = self.store.create_session(
            kind, hours, request.headers.get("user-agent", ""), remember
        )
        response.set_cookie(
            COOKIE,
            token,
            max_age=int(hours * 3600) if remember else None,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    def check_rate(self) -> None:
        now = time.monotonic()
        self.failures = [moment for moment in self.failures if now - moment < LOCK_SECONDS]
        if len(self.failures) >= MAX_FAILURES:
            raise HTTPException(429, "Too many attempts. Wait five minutes and try again.")

    def failed(self) -> None:
        self.failures.append(time.monotonic())


def install_auth(app: FastAPI, settings, same_origin, api_token_valid) -> AuthService:
    auth = AuthService(settings)
    app.state.auth = auth

    def browser_request(request: Request) -> str:
        """State changes must come from the dashboard page itself."""
        same_origin(request)
        origin = request.headers.get("origin")
        if not origin:
            raise HTTPException(403, "Use the Bridge dashboard to do this.")
        return origin

    def require_owner(request: Request):
        current = auth.owner_session(request)
        if current is None:
            raise HTTPException(401, "Sign in to continue.")
        return current

    @app.get("/api/v1/auth/status", include_in_schema=False)
    async def status(request: Request):
        owner = auth.store.owner()
        current = auth.session(request)
        signed_in = bool(current and current.kind == "owner" and owner)
        linked = {item["provider"] for item in auth.store.identities()}
        origin = f"{request.url.scheme}://{request.url.netloc}"
        return {
            "has_owner": owner is not None,
            "signed_in": signed_in,
            "setup": bool(current and current.kind == "setup" and owner is None),
            "owner": {"name": owner.name, "email": owner.email} if signed_in else None,
            "methods": {
                "password": bool(owner and owner.has_password),
                "passkey": bool(auth.store.passkeys()),
                **{
                    provider: {
                        "configured": auth.oauth.configured(provider),
                        "linked": provider in linked,
                    }
                    for provider in PROVIDERS
                },
            },
            "passkeys_supported": origin.startswith("http://localhost:"),
        }

    @app.post("/api/v1/auth/signup", include_in_schema=False)
    async def signup(payload: SignUp, request: Request):
        browser_request(request)
        current = auth.session(request)
        if not ((current and current.kind == "setup") or api_token_valid(request)):
            raise HTTPException(
                403, "Open the dashboard from the Bridge menu bar to create your account."
            )
        try:
            owner = auth.store.create_owner(
                checked_email(payload.email), payload.name.strip(), payload.password
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        auth.store.end_session(request.cookies.get(COOKIE))
        response = JSONResponse(
            {"signed_in": True, "owner": {"name": owner.name, "email": owner.email}}
        )
        return auth.sign_in(response, request, remember=True)

    @app.post("/api/v1/auth/login", include_in_schema=False)
    async def login(payload: Login, request: Request):
        browser_request(request)
        auth.check_rate()
        if not auth.store.check_password(payload.email.strip(), payload.password):
            auth.failed()
            raise HTTPException(401, "That email and password don't match.")
        auth.failures.clear()
        return auth.sign_in(JSONResponse({"signed_in": True}), request, remember=payload.remember)

    @app.post("/api/v1/auth/logout", include_in_schema=False)
    async def logout(request: Request):
        browser_request(request)
        auth.store.end_session(request.cookies.get(COOKIE))
        response = JSONResponse({"signed_in": False})
        response.delete_cookie(COOKIE, path="/")
        return response

    # Google / GitHub ----------------------------------------------------------------------

    @app.post("/api/v1/auth/oauth/{provider}/start", include_in_schema=False)
    async def oauth_start(provider: str, payload: Start, request: Request):
        origin = browser_request(request)
        current = auth.session(request)
        owner = auth.store.owner()
        if payload.intent == "signup":
            if owner is not None:
                raise HTTPException(409, "Bridge already has an owner account. Sign in instead.")
            if not ((current and current.kind == "setup") or api_token_valid(request)):
                raise HTTPException(403, "Open the dashboard from the Bridge menu bar first.")
        elif payload.intent == "link":
            if current is None or current.kind != "owner":
                raise HTTPException(401, "Sign in to link another account.")
        elif owner is None:
            raise HTTPException(409, "Create your Bridge account first.")
        try:
            url = auth.oauth.start(
                provider,
                payload.intent + (":remember" if payload.remember else ""),
                origin,
                current.id if current else None,
            )
        except SignInError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"url": url}

    @app.get("/api/v1/auth/oauth/{provider}/callback", include_in_schema=False)
    async def oauth_callback(
        provider: str,
        request: Request,
        state: str = Query(default="", max_length=200),
        code: str = Query(default="", max_length=4096),
        error: str = Query(default="", max_length=200),
    ):
        def fail(message: str):
            return RedirectResponse("/#auth-error=" + quote(message), status_code=303)

        try:
            pending = auth.oauth.take(provider, state)
            if error or not code:
                return fail("Sign-in was cancelled.")
            identity = await auth.oauth.finish(pending, code)
        except SignInError as exc:
            return fail(str(exc))
        intent, _, remember = pending.intent.partition(":")
        name = PROVIDERS[provider]["name"]
        store = auth.store
        if intent == "link":
            linked_by = next((s for s in store.sessions() if s.id == pending.session_id), None)
            if linked_by is None:
                return fail("Your session ended. Sign in and try linking again.")
            store.link_identity(provider, identity.subject, identity.email, identity.login)
            return RedirectResponse(f"/#account-linked={provider}", status_code=303)
        if intent == "signup":
            if store.owner() is not None:
                return fail("Bridge already has an owner account. Sign in instead.")
            store.create_owner(identity.email, identity.name, None)
            store.link_identity(provider, identity.subject, identity.email, identity.login)
            if pending.session_id:
                store.end_session(session_id=pending.session_id)
            return auth.sign_in(RedirectResponse("/", status_code=303), request, remember=True)
        if not store.has_identity(provider, identity.subject):
            return fail(
                f"This {name} account isn't linked to your Bridge. Sign in another way, "
                "then link it on the Account page."
            )
        return auth.sign_in(
            RedirectResponse("/", status_code=303), request, remember=remember == "remember"
        )

    # Passkeys -----------------------------------------------------------------------------

    @app.post("/api/v1/auth/passkeys/options", include_in_schema=False)
    async def passkey_options(request: Request):
        browser_request(request)
        require_owner(request)
        return auth.passkeys.registration_options(auth.store.owner())

    @app.post("/api/v1/auth/passkeys", include_in_schema=False)
    async def passkey_register(payload: PasskeyRegistration, request: Request):
        origin = browser_request(request)
        require_owner(request)
        try:
            return auth.passkeys.register(payload.credential, origin, payload.name.strip())
        except PasskeyError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/v1/auth/passkey-login/options", include_in_schema=False)
    async def passkey_login_options(request: Request):
        browser_request(request)
        if not auth.store.passkeys():
            raise HTTPException(409, "No passkey is set up yet.")
        return auth.passkeys.authentication_options()

    @app.post("/api/v1/auth/passkey-login", include_in_schema=False)
    async def passkey_login(payload: PasskeyLogin, request: Request):
        origin = browser_request(request)
        auth.check_rate()
        try:
            auth.passkeys.authenticate(payload.credential, origin)
        except PasskeyError as exc:
            auth.failed()
            raise HTTPException(401, str(exc)) from None
        return auth.sign_in(JSONResponse({"signed_in": True}), request, remember=payload.remember)

    # Account ------------------------------------------------------------------------------

    @app.get("/api/v1/auth/account", include_in_schema=False)
    async def account(request: Request):
        current = require_owner(request)
        owner = auth.store.owner()
        return {
            "owner": {
                "name": owner.name,
                "email": owner.email,
                "has_password": owner.has_password,
                "created_at": owner.created_at,
            },
            "identities": [
                {key: item[key] for key in ("provider", "email", "login", "created_at")}
                for item in auth.store.identities()
            ],
            "providers": {p: auth.oauth.configured(p) for p in PROVIDERS},
            "passkeys": [
                {key: item[key] for key in ("credential_id", "name", "created_at", "last_used")}
                for item in auth.store.passkeys()
            ],
            "sessions": [
                {
                    "id": item.id,
                    "current": item.id == current.id,
                    "created_at": item.created_at,
                    "last_seen": item.last_seen,
                    "user_agent": item.user_agent,
                    "remember": item.remember,
                }
                for item in auth.store.sessions()
            ],
        }

    def keep_one_method():
        if auth.store.sign_in_methods() <= 1:
            raise HTTPException(409, "Keep at least one way to sign in.")

    @app.post("/api/v1/auth/profile", include_in_schema=False)
    async def profile(payload: Profile, request: Request):
        browser_request(request)
        require_owner(request)
        auth.store.update_profile(checked_email(payload.email), payload.name.strip())
        return {"saved": True}

    @app.post("/api/v1/auth/password", include_in_schema=False)
    async def password(payload: PasswordChange, request: Request):
        browser_request(request)
        require_owner(request)
        owner = auth.store.owner()
        if owner.has_password and not auth.store.check_password(owner.email, payload.current):
            auth.failed()
            raise HTTPException(401, "Your current password isn't right.")
        auth.store.set_password(payload.new)
        return {"saved": True}

    @app.post("/api/v1/auth/unlink", include_in_schema=False)
    async def unlink(payload: Target, request: Request):
        browser_request(request)
        require_owner(request)
        keep_one_method()
        auth.store.unlink_identity(payload.value)
        return {"removed": True}

    @app.post("/api/v1/auth/passkeys/remove", include_in_schema=False)
    async def passkey_remove(payload: Target, request: Request):
        browser_request(request)
        require_owner(request)
        keep_one_method()
        if not auth.store.remove_passkey(payload.value):
            raise HTTPException(404, "That passkey was already removed.")
        return {"removed": True}

    @app.post("/api/v1/auth/sessions/end", include_in_schema=False)
    async def end_session(payload: Target, request: Request):
        browser_request(request)
        require_owner(request)
        auth.store.end_session(session_id=payload.value)
        return {"ended": True}

    @app.post("/api/v1/auth/sessions/end-others", include_in_schema=False)
    async def end_others(request: Request):
        browser_request(request)
        require_owner(request)
        return {"ended": auth.store.end_other_sessions(request.cookies.get(COOKIE, ""))}

    return auth
