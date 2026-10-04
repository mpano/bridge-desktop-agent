"""Work tools connected with a personal token: Jira + Confluence (Atlassian) and GitHub.

These services don't need an OAuth app of your own: you paste an API token you created
(or, for GitHub, Bridge asks the GitHub command line you're already signed in to). Tokens
live in their own macOS Keychain entry, never in .env or the database, and never reach the
model or the page. Requests go only to your Atlassian site or to api.github.com.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import time
from urllib.parse import quote

import httpx

from app.integrations.models import IntegrationError

SITE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.atlassian\.net$")
GH_CANDIDATES = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh")
GH_TOKEN_SECONDS = 600
TOKEN_PROVIDERS = {
    "jira": {
        "name": "Jira & Confluence",
        "reads": "your issues and projects, Confluence pages you can see",
        "actions": "creating issues, commenting and moving status",
    },
    "github": {
        "name": "GitHub",
        "reads": "pull requests, reviews, issues and checks",
        "actions": "creating issues and commenting",
    },
}


def gh_path() -> str | None:
    return next((path for path in GH_CANDIDATES if os.access(path, os.X_OK)), None)


class KeychainTokens:
    """One Keychain entry, owned by Bridge.app, holding the token connections."""

    service = "Bridge Connected Services"
    username = "tokens-v1"

    def _backend(self):
        if sys.platform != "darwin":
            raise IntegrationError("Work connections need the macOS Keychain.")
        from keyring.backends.macOS import Keyring

        return Keyring()

    def load(self) -> dict:
        try:
            return json.loads(self._backend().get_password(self.service, self.username) or "{}")
        except IntegrationError:
            raise
        except Exception:
            raise IntegrationError(
                "Couldn't read work connections from the Keychain. If macOS asked, choose "
                "Always Allow."
            ) from None

    def save(self, data: dict) -> None:
        try:
            self._backend().set_password(self.service, self.username, json.dumps(data))
        except Exception:
            raise IntegrationError("Couldn't save to the Keychain. Try again.") from None


class MemoryTokens:
    def __init__(self):
        self.data: dict = {}

    def load(self) -> dict:
        return json.loads(json.dumps(self.data))

    def save(self, data: dict) -> None:
        self.data = json.loads(json.dumps(data))


class TokenConnections:
    def __init__(self, store=None, activity=None, http: httpx.AsyncClient | None = None):
        self.store = store or KeychainTokens()
        self.activity = activity
        self.http = http
        self.lock = asyncio.Lock()
        self._gh: tuple[float, str] | None = None

    # Connecting --------------------------------------------------------------------------

    def _record(self, provider: str, event: str, ok: bool = True, detail: str = "", who: str = ""):
        if self.activity is not None:
            self.activity.record(provider, event, ok=ok, detail=detail, account=who)

    async def accounts(self) -> dict:
        async with self.lock:
            data = await asyncio.to_thread(self.store.load)
        return data

    async def catalog(self) -> list[dict]:
        data = await self.accounts()
        listed = []
        for key, info in TOKEN_PROVIDERS.items():
            item = data.get(key)
            listed.append(
                {
                    "provider": key,
                    "name": info["name"],
                    "kind": "token",
                    "configured": True,
                    "reads": info["reads"],
                    "actions_text": info["actions"],
                    "cli_available": key == "github" and gh_path() is not None,
                    "account": (
                        {
                            "account_id": key,
                            "provider": key,
                            "identity": item["identity"],
                            "site": item.get("site", ""),
                            "method": item.get("method", "token"),
                            "actions": item.get("actions", False),
                            "connected_at": item.get("connected_at", 0),
                        }
                        if item
                        else None
                    ),
                }
            )
        return listed

    async def connect(
        self,
        provider: str,
        *,
        site: str = "",
        email: str = "",
        token: str = "",
        use_cli: bool = False,
        actions: bool = True,
    ) -> dict:
        if provider not in TOKEN_PROVIDERS:
            raise IntegrationError("Unknown service.")
        entry: dict = {"actions": bool(actions), "connected_at": time.time()}
        if provider == "jira":
            site = site.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
            if not SITE.fullmatch(site):
                raise IntegrationError("Use your Atlassian address, like yourteam.atlassian.net.")
            if not email.strip() or not token.strip():
                raise IntegrationError("Enter your Atlassian email and API token.")
            entry |= {"site": site, "email": email.strip(), "token": token.strip()}
        elif use_cli:
            if gh_path() is None:
                raise IntegrationError("The GitHub command line (gh) isn't installed.")
            entry |= {"method": "cli"}
            self._gh = None
        else:
            if not token.strip():
                raise IntegrationError("Paste a GitHub token.")
            entry |= {"method": "token", "token": token.strip()}
        # Check it works before saving, and learn who you are there.
        try:
            me = await self._call(
                provider, entry, "GET", "myself" if provider == "jira" else "user"
            )
        except IntegrationError as exc:
            self._record(provider, "Connection failed", ok=False, detail=str(exc))
            raise
        entry["identity"] = (
            me.get("emailAddress") or me.get("displayName") or entry.get("email", "")
            if provider == "jira"
            else me.get("login", "")
        )
        entry["user_id"] = me.get("accountId") if provider == "jira" else me.get("login")
        async with self.lock:
            data = await asyncio.to_thread(self.store.load)
            data[provider] = entry
            await asyncio.to_thread(self.store.save, data)
        self._record(provider, "Connected", who=entry["identity"])
        return {"connected": True, "identity": entry["identity"]}

    async def disconnect(self, provider: str) -> dict:
        async with self.lock:
            data = await asyncio.to_thread(self.store.load)
            if provider not in data:
                raise IntegrationError("Not connected.")
            removed = data.pop(provider)
            await asyncio.to_thread(self.store.save, data)
        self._record(provider, "Disconnected", who=removed.get("identity", ""))
        what = "your API token at id.atlassian.com" if provider == "jira" else "the token on GitHub"
        return {"message": f"Removed from Bridge. You can also revoke {what}."}

    # Calling ---------------------------------------------------------------------------------

    async def _gh_token(self) -> str:
        if self._gh and time.monotonic() - self._gh[0] < GH_TOKEN_SECONDS:
            return self._gh[1]
        path = gh_path()
        if path is None:
            raise IntegrationError("The GitHub command line (gh) isn't installed anymore.")
        process = await asyncio.create_subprocess_exec(
            path,
            "auth",
            "token",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "GH_PROMPT_DISABLED": "1"},
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(), 15)
        except TimeoutError:
            process.kill()
            raise IntegrationError("The GitHub command line didn't answer.") from None
        token = out.decode().strip()
        if process.returncode != 0 or not token:
            raise IntegrationError("Sign in to the GitHub command line again: gh auth login.")
        self._gh = (time.monotonic(), token)
        return token

    async def _call(self, provider: str, entry: dict, method: str, path: str, **kwargs):
        # Paths are built by Bridge from encoded IDs, never taken whole from anywhere.
        if path.startswith(("/", "http:", "https:")) or ".." in path or "#" in path:
            raise IntegrationError("Invalid resource path.")
        headers = {"Accept": "application/json", "User-Agent": "Bridge"}
        if provider == "jira":
            api = "wiki/" if path.startswith("wiki/") else "rest/api/3/"
            url = f"https://{entry['site']}/{api}{path.removeprefix('wiki/')}"
            pair = f"{entry['email']}:{entry['token']}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
        else:
            url = f"https://api.github.com/{path}"
            token = await self._gh_token() if entry.get("method") == "cli" else entry["token"]
            headers["Authorization"] = f"Bearer {token}"
            headers["Accept"] = "application/vnd.github+json"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        client = self.http or httpx.AsyncClient(timeout=20, follow_redirects=False)
        try:
            response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError:
            raise IntegrationError(f"Couldn't reach {TOKEN_PROVIDERS[provider]['name']}.") from None
        finally:
            if self.http is None:
                await client.aclose()
        if response.status_code in (401, 403):
            if provider == "github" and entry.get("method") == "cli":
                self._gh = None
            raise IntegrationError(
                "The token was refused. Check it, or connect again in Connections."
                if response.status_code == 401
                else "You don't have permission for that."
            )
        if response.status_code == 404:
            raise IntegrationError("Not found, or you can't see it.")
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                messages = body.get("errorMessages") or list((body.get("errors") or {}).values())
                detail = (
                    "; ".join(str(m) for m in messages)[:200] or str(body.get("message", ""))[:200]
                )
            except ValueError:
                pass
            raise IntegrationError(detail or f"The request failed ({response.status_code}).")
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    async def request(self, provider: str, method: str, path: str, *, write=False, **kwargs):
        data = await self.accounts()
        entry = data.get(provider)
        if entry is None:
            name = TOKEN_PROVIDERS[provider]["name"]
            raise IntegrationError(f"{name} isn't connected. Connect it in Connections.")
        if write and not entry.get("actions"):
            raise IntegrationError(
                "This connection is read-only. Reconnect with actions allowed in Connections."
            )
        try:
            result = await self._call(provider, entry, method, path, **kwargs)
        except IntegrationError as exc:
            self._record(provider, describe(provider, method, path), ok=False, detail=str(exc))
            raise
        self._record(provider, describe(provider, method, path), who=entry.get("identity", ""))
        return result

    async def site(self) -> str:
        return (await self.accounts()).get("jira", {}).get("site", "")

    async def me(self, provider: str) -> str:
        return (await self.accounts()).get(provider, {}).get("user_id", "")


def describe(provider: str, method: str, path: str) -> str:
    """What happened, for the Connections activity list (never the content)."""
    if provider == "jira":
        if path.startswith("wiki/"):
            return "Searched Confluence" if "search" in path else "Read a Confluence page"
        if method == "POST" and path.endswith("/comment"):
            return "Commented on an issue"
        if method == "POST" and path.endswith("/transitions"):
            return "Moved an issue"
        if method == "POST" and path == "issue":
            return "Created an issue"
        if path.startswith("search"):
            return "Searched issues"
        return "Read Jira"
    if method == "POST" and path.endswith("/comments"):
        return "Commented"
    if method == "POST" and path.endswith("/issues"):
        return "Created an issue"
    if path.startswith("search"):
        return "Searched GitHub"
    return "Read GitHub"


def enc(value: str) -> str:
    return quote(str(value), safe="")
