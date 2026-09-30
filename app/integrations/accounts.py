"""OAuth state, PKCE, token rotation and scoped account access."""

import asyncio
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from pydantic import SecretStr

from app.integrations.activity import MemoryActivity, describe
from app.integrations.credentials import CredentialStore, KeychainCredentials
from app.integrations.models import Account, IntegrationError
from app.integrations.providers import PROVIDERS
from app.integrations.transport import ProviderHTTP


@dataclass
class PendingConnection:
    provider: str
    redirect_uri: str
    scopes: list[str]
    expires_at: float
    verifier: str = field(repr=False)


class AccountManager:
    def __init__(self, settings, store: CredentialStore | None = None, http=None, activity=None):
        self.settings = settings
        self.store = store or KeychainCredentials()
        self.http = http or ProviderHTTP()
        self.activity = activity or MemoryActivity()
        self.lock = asyncio.Lock()
        self.pending: dict[str, PendingConnection] = {}

    def client_id(self, provider: str) -> str:
        key = "google" if provider in {"gmail", "google_calendar"} else provider
        return getattr(self.settings, key + "_client_id")

    def redirect_uri(self, provider: str) -> str:
        host = "localhost" if provider == "slack" else "127.0.0.1"
        return f"http://{host}:{self.settings.integrations_callback_port}/api/v1/connections/callback/{provider}"

    def _enabled(self):
        if not self.settings.integrations_enabled:
            raise IntegrationError(
                "Set INTEGRATIONS_ENABLED=true and restart Bridge to connect accounts."
            )

    async def catalog(self) -> dict:
        specs = [
            {
                "provider": key,
                "name": value.name,
                "configured": bool(self.client_id(key)),
                "redirect_uri": self.redirect_uri(key),
                "read_scopes": list(value.read_scopes),
                "write_scopes": list(value.write_scopes),
            }
            for key, value in PROVIDERS.items()
        ]
        if not self.settings.integrations_enabled:
            return {
                "enabled": False,
                "providers": specs,
                "accounts": [],
                "message": "Enable connected services in .env to get started.",
            }
        async with self.lock:
            accounts = await asyncio.to_thread(self.store.load)
        return {
            "enabled": True,
            "providers": specs,
            "accounts": [value.public() for value in accounts.values()],
            "activity": self.activity.recent(100),
        }

    async def list_accounts(self) -> dict:
        self._enabled()
        async with self.lock:
            values = await asyncio.to_thread(self.store.load)
        return {"accounts": [value.public() for value in values.values()]}

    async def begin(self, provider: str, allow_actions: bool) -> dict:
        self._enabled()
        if provider not in PROVIDERS or not self.client_id(provider):
            raise IntegrationError(
                "Configure this provider's OAuth client ID in .env and restart Bridge."
            )
        # Fail before browser consent if the Keychain backend is unavailable.
        async with self.lock:
            await asyncio.to_thread(self.store.load)
            now = time.monotonic()
            self.pending = {
                key: value for key, value in self.pending.items() if value.expires_at > now
            }
            if len(self.pending) >= 10:
                raise IntegrationError(
                    "Too many unfinished connections. Wait five minutes and retry."
                )
            spec = PROVIDERS[provider]
            scopes = list(spec.read_scopes + (spec.write_scopes if allow_actions else ()))
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
            redirect = self.redirect_uri(provider)
            self.pending[state] = PendingConnection(provider, redirect, scopes, now + 300, verifier)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            params = {
                "client_id": self.client_id(provider),
                "response_type": "code",
                "redirect_uri": redirect,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
            params["user_scope" if provider == "slack" else "scope"] = (
                "," if provider == "slack" else " "
            ).join(scopes)
            if provider in {"gmail", "google_calendar"}:
                params.update(access_type="offline", prompt="consent")
            self.activity.record(
                provider,
                "Sign-in started",
                detail="read and actions" if allow_actions else "read only",
            )
            return {
                "authorization_url": spec.authorization_url + "?" + urlencode(params),
                "expires_in_seconds": 300,
            }

    def _token_data(self, provider: str, data: dict) -> dict:
        if provider == "slack" and "authed_user" in data:
            data = data["authed_user"]
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("access_token"), str)
            or not data["access_token"]
        ):
            raise IntegrationError(
                "Provider did not return a usable user token. Check OAuth configuration."
            )
        if data.get("token_type", "bearer").lower() not in {"bearer", "user"}:
            raise IntegrationError("Unexpected provider token type.")
        return data

    def _credentials(self, provider: str) -> dict:
        result = {"client_id": self.client_id(provider)}
        if (
            provider in {"gmail", "google_calendar"}
            and self.settings.google_client_secret.get_secret_value()
        ):
            result["client_secret"] = self.settings.google_client_secret.get_secret_value()
        return result

    async def finish(
        self, provider: str, state: str, code: str | None, error: str | None = None
    ) -> dict:
        try:
            account = await self._finish(provider, state, code, error)
        except IntegrationError as exc:
            if provider in PROVIDERS:
                self.activity.record(provider, "Sign-in failed", ok=False, detail=str(exc))
            raise
        self.activity.record(provider, "Connected", account=account["identity"])
        return account

    async def _finish(
        self, provider: str, state: str, code: str | None, error: str | None = None
    ) -> dict:
        self._enabled()
        async with self.lock:
            pending = self.pending.get(state)
            if (
                pending is None
                or pending.provider != provider
                or pending.expires_at <= time.monotonic()
            ):
                raise IntegrationError(
                    "Connection request expired, mismatched, or already used. Start again in "
                    "Connections."
                )
            del self.pending[state]  # Single use, including denied or failed exchanges.
            if error or not code:
                raise IntegrationError("Connection was not authorized. Start again when ready.")
            spec = PROVIDERS[provider]
            payload = {
                **self._credentials(provider),
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": pending.redirect_uri,
                "code_verifier": pending.verifier,
            }
            data = self._token_data(
                provider, await self.http.request("POST", spec.token_url, data=payload)
            )
            access = data["access_token"]
            if provider == "gmail":
                identity = (
                    await self.http.request("GET", spec.api_base + "profile", token=access)
                )["emailAddress"]
            elif provider == "google_calendar":
                identity = (
                    await self.http.request(
                        "GET", "https://openidconnect.googleapis.com/v1/userinfo", token=access
                    )
                )["email"]
            elif provider == "spotify":
                identity = (await self.http.request("GET", spec.api_base + "me", token=access))[
                    "id"
                ]
            else:
                profile = await self.http.request("POST", spec.api_base + "auth.test", token=access)
                identity = profile["team_id"] + "/" + profile["user_id"]
            if (
                not isinstance(identity, str)
                or not identity
                or len(identity) > 320
                or any(ord(c) < 32 for c in identity)
            ):
                raise IntegrationError("Provider returned an invalid account identity.")
            scopes = (
                data.get("scope", ("," if provider == "slack" else " ").join(pending.scopes))
                .replace(",", " ")
                .split()
            )
            account = Account(
                account_id=f"{provider}:{identity}",
                provider=provider,
                identity=identity,
                scopes=scopes,
                client_id=self.client_id(provider),
                access_token=access,
                refresh_token=data.get("refresh_token", ""),
                expires_at=time.time() + float(data.get("expires_in", 3600)),
                connected_at=time.time(),
            )
            accounts = await asyncio.to_thread(self.store.load)
            # Reconnecting without a refresh token must not borrow one from a different client.
            previous = accounts.get(account.account_id)
            if (
                previous
                and previous.client_id == account.client_id
                and not account.refresh_token.get_secret_value()
            ):
                account.refresh_token = previous.refresh_token
            accounts[account.account_id] = account
            await asyncio.to_thread(self.store.save, accounts)
            return account.public()

    async def disconnect(self, account_id: str) -> dict:
        self._enabled()
        async with self.lock:
            accounts = await asyncio.to_thread(self.store.load)
            if account_id not in accounts:
                raise IntegrationError("Account is not connected.")
            removed = accounts.pop(account_id)
            await asyncio.to_thread(self.store.save, accounts)
        self.activity.record(removed.provider, "Disconnected", account=removed.identity)
        return {
            "message": (
                "Removed this account's credentials from Bridge. Revoke the app in the provider's "
                "account settings to remove its grant there too."
            )
        }

    @staticmethod
    def _select(accounts: dict[str, Account], account_id: str | None, provider: str) -> Account:
        """Use the named account, or the only connected account for this service.

        The model cannot see account IDs when tool results are withheld, so an omitted
        ID (or the account's email/identity) is resolved here instead of guessed.
        """
        name = PROVIDERS[provider].name
        candidates = [value for value in accounts.values() if value.provider == provider]
        if account_id:
            matches = [
                value for value in candidates if account_id in {value.account_id, value.identity}
            ]
        else:
            matches = candidates
        if len(matches) == 1:
            return matches[0]
        if not candidates:
            raise IntegrationError(
                f"No {name} account is connected. Connect one in the dashboard's Connections page."
            )
        if not matches:
            raise IntegrationError(f"That {name} account is not connected to Bridge.")
        identities = ", ".join(sorted(value.identity for value in matches))
        raise IntegrationError(f"Several {name} accounts are connected ({identities}). Say which.")

    async def request(
        self,
        account_id: str | None,
        provider: str,
        method: str,
        path: str,
        *,
        scopes: tuple[str, ...] = (),
        **kwargs,
    ) -> dict:
        """Call the provider API, and log the action (never its content) for Connections."""
        used = {}
        try:
            result = await self._request(account_id, provider, method, path, scopes, used, **kwargs)
        except IntegrationError as exc:
            action = describe(provider, method, path)
            self.activity.record(
                provider, action, ok=False, detail=str(exc), account=used.get("identity", "")
            )
            raise
        self.activity.record(
            provider, describe(provider, method, path), account=used.get("identity", "")
        )
        return result

    async def _request(
        self,
        account_id: str | None,
        provider: str,
        method: str,
        path: str,
        scopes: tuple[str, ...],
        used: dict,
        **kwargs,
    ) -> dict:
        self._enabled()
        # Paths are constants or encoded individual IDs supplied by provider adapters, never URLs.
        if path.startswith(("/", "http:", "https:")) or ".." in path or "?" in path or "#" in path:
            raise IntegrationError("Invalid provider resource path.")
        async with self.lock:
            accounts = await asyncio.to_thread(self.store.load)
            account = self._select(accounts, account_id, provider)
            used["identity"] = account.identity
            if account.client_id != self.client_id(provider):
                raise IntegrationError(
                    "OAuth client configuration changed. Reconnect this account."
                )
            if not set(scopes).issubset(account.scopes):
                raise IntegrationError(
                    "This connection lacks the required permissions. Reconnect with actions enabled"
                    " if needed."
                )
            spec = PROVIDERS[provider]
            if account.expires_at <= time.time() + 60:
                refresh = account.refresh_token.get_secret_value()
                if not refresh:
                    raise IntegrationError("Account access expired. Reconnect in Connections.")
                data = self._token_data(
                    provider,
                    await self.http.request(
                        "POST",
                        spec.token_url,
                        data={
                            **self._credentials(provider),
                            "grant_type": "refresh_token",
                            "refresh_token": refresh,
                        },
                    ),
                )
                account.access_token = SecretStr(data["access_token"])
                account.refresh_token = SecretStr(data.get("refresh_token", refresh))
                account.expires_at = time.time() + float(data.get("expires_in", 3600))
                if "scope" in data:
                    account.scopes = data["scope"].replace(",", " ").split()
                await asyncio.to_thread(self.store.save, accounts)
                self.activity.record(provider, "Renewed access", account=account.identity)
                if not set(scopes).issubset(account.scopes):
                    raise IntegrationError("Provider permissions changed. Reconnect this account.")
            return await self.http.request(
                method,
                spec.api_base + path,
                token=account.access_token.get_secret_value(),
                **kwargs,
            )
