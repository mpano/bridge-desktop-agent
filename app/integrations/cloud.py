"""This Mac's Bridge account: a free trial, plans and credit (served by Bridge Cloud).

The account is this Mac's token, kept in the Keychain. Buying happens on Lemon Squeezy's
checkout page in your browser; Bridge never sees card details. To use the same plan on
another Mac, enter the license key from the purchase email there.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
from pydantic import SecretStr

from app.config.env_file import update_env

SERVICE, ACCOUNT = "Bridge Cloud", "device-token"


class CloudError(RuntimeError):
    """Safe to show to the user."""


class KeychainVault:
    def load(self) -> str:
        try:
            import keyring

            return keyring.get_password(SERVICE, ACCOUNT) or ""
        except Exception:
            return ""

    def save(self, token: str) -> None:
        import keyring

        keyring.set_password(SERVICE, ACCOUNT, token)

    def clear(self) -> None:
        try:
            import keyring

            keyring.delete_password(SERVICE, ACCOUNT)
        except Exception:
            pass


def load_token(settings, vault=None) -> bool:
    """At startup: this Mac's Bridge account token, from the Keychain."""
    token = (vault or KeychainVault()).load()
    if token:
        settings.cloud_token = SecretStr(token)
    return bool(token)


class Cloud:
    def __init__(self, settings, *, vault=None, http: httpx.AsyncClient | None = None, env=None):
        self.settings = settings
        self.vault = vault or KeychainVault()
        self.http = http
        self.env = env  # The .env file to remember the choice in (None: the current one).

    @property
    def token(self) -> str:
        return self.settings.cloud_token.get_secret_value()

    async def _call(self, method: str, path: str, *, json: dict | None = None, auth=True) -> dict:
        url = self.settings.cloud_url.rstrip("/") + path
        headers = {"Authorization": f"Bearer {self.token}"} if auth else {}
        client = self.http or httpx.AsyncClient(timeout=20)
        try:
            response = await client.request(method, url, json=json, headers=headers)
        except httpx.HTTPError:
            raise CloudError("Couldn't reach Bridge's servers. Are you online?") from None
        finally:
            if self.http is None:
                await client.aclose()
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code == 401 and auth:
            raise CloudError("This Mac isn't signed in to a Bridge plan any more.")
        if response.status_code >= 400:
            raise CloudError(
                str(data.get("detail") or f"Bridge's servers answered {response.status_code}.")
            )
        return data

    async def _remember(self, provider: str) -> None:
        self.settings.ai_provider = provider
        from app.api.app_settings import env_path

        try:
            await asyncio.to_thread(update_env, self.env or env_path(), {"AI_PROVIDER": provider})
        except OSError:
            pass  # Works until Bridge restarts.

    # What the Settings screen does -----------------------------------------------------------

    async def overview(self) -> dict | None:
        if not self.token:
            return None
        return await self._call("GET", "/v1/account")

    async def start(self) -> dict:
        """Use a Bridge plan on this Mac (a free trial the first time)."""
        if self.token:
            data = await self._call("GET", "/v1/account")
        else:
            name = await asyncio.to_thread(socket.gethostname)
            data = await self._call("POST", "/v1/devices", json={"name": name[:80]}, auth=False)
            token = str(data.pop("token", ""))
            if not token:
                raise CloudError("Bridge's servers didn't create an account. Try again.")
            await asyncio.to_thread(self.vault.save, token)
            self.settings.cloud_token = SecretStr(token)
        await self._remember("bridge")
        return data

    async def use_own_key(self) -> None:
        await self._remember("own_key")

    async def checkout(self, plan: str) -> str:
        return (await self._call("POST", "/v1/checkout", json={"plan": plan}))["url"]

    async def portal(self) -> str:
        return (await self._call("POST", "/v1/portal"))["url"]

    async def link(self, license_key: str) -> dict:
        if not self.token:
            await self.start()
        data = await self._call("POST", "/v1/link", json={"license_key": license_key.strip()})
        await self._remember("bridge")
        return data

    async def sign_out(self) -> None:
        if self.token:
            try:
                await self._call("POST", "/v1/sign-out")
            except CloudError:
                pass
        await asyncio.to_thread(self.vault.clear)
        self.settings.cloud_token = SecretStr("")
        await self._remember("own_key")

    async def voice_ended(self, voice_id: str) -> None:
        if voice_id and self.token:
            try:
                await self._call("POST", "/v1/voice/end", json={"voice_id": voice_id})
            except CloudError:
                pass  # Charged in full later; nothing to tell the user now.
