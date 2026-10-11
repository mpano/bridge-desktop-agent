"""Your MCP apps: which are connected, signing in, their tools, and using them safely.

Bridge gives the model two tools however many apps you connect (OpenAI allows 128 tools per
request, and Bridge already has most of those): `apps_find` lists what your apps can do, and
`apps_use` runs one. Each use is judged on its own: tools the app marks read-only run
directly; anything else asks you first, and you can change that per tool or turn tools off.
Sign-ins, API keys and tokens live in the Keychain; what an app returns reaches the model only
when you share that app's results (Connections), like your other services.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.integrations.mcp import oauth
from app.integrations.mcp.client import (
    HTTPClient,
    MCPError,
    NeedsSignIn,
    SSEClient,
    StdioClient,
    result_text,
)
from app.integrations.tokens import KeychainTokens

PRESETS = [
    {
        "id": "notion",
        "name": "Notion",
        "url": "https://mcp.notion.com/mcp",
        "what": "Search and edit your pages and databases.",
    },
    {
        "id": "linear",
        "name": "Linear",
        "url": "https://mcp.linear.app/mcp",
        "what": "Issues, projects and cycles.",
    },
    {
        "id": "sentry",
        "name": "Sentry",
        "url": "https://mcp.sentry.dev/mcp",
        "what": "Errors and issues in your apps.",
    },
    {
        "id": "figma",
        "name": "Figma",
        "url": "https://mcp.figma.com/mcp",
        "what": "Your designs, components and comments.",
    },
    {
        "id": "atlassian",
        "name": "Atlassian (Rovo)",
        "url": "https://mcp.atlassian.com/v1/sse",
        "what": "Jira and Confluence, with Atlassian's own tools.",
    },
    {
        "id": "asana",
        "name": "Asana",
        "url": "https://mcp.asana.com/sse",
        "what": "Tasks and projects.",
    },
    {
        "id": "stripe",
        "name": "Stripe",
        "url": "https://mcp.stripe.com",
        "what": "Customers, payments and invoices.",
    },
    {
        "id": "supabase",
        "name": "Supabase",
        "url": "https://mcp.supabase.com/mcp",
        "what": "Your databases and projects.",
    },
    {
        "id": "vercel",
        "name": "Vercel",
        "url": "https://mcp.vercel.com",
        "what": "Deployments and projects.",
    },
    {"id": "canva", "name": "Canva", "url": "https://mcp.canva.com/mcp", "what": "Your designs."},
    {
        "id": "huggingface",
        "name": "Hugging Face",
        "url": "https://hf.co/mcp",
        "what": "Models, datasets and papers.",
    },
]
MODES = ("auto", "ask", "off")  # Per tool: as the app says / always ask first / never use.


class MCPSecrets(KeychainTokens):
    username = "mcp-v1"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "app"


def _check_url(url: str) -> str:
    parts = urlsplit(url.strip())
    local = parts.hostname in {"localhost", "127.0.0.1"}
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise MCPError("Use an https address (or http://localhost for a server on this Mac).")
    if not parts.netloc:
        raise MCPError("That isn't a web address.")
    return url.strip()


class MCPManager:
    def __init__(
        self, path, redirect_uri: str, secrets_store=None, http_factory=None, clock=time.time
    ):
        self.path = str(path)
        self.redirect_uri = redirect_uri
        self.secrets = secrets_store or MCPSecrets()
        self.http_factory = http_factory or (
            lambda: httpx.AsyncClient(timeout=60, follow_redirects=False)
        )
        self.clock = clock
        self.clients: dict[str, object] = {}
        self.tools: dict[str, list[dict]] = {}
        self.status: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}  # OAuth state → what to finish.
        self.privacy = None  # Set by bootstrap: shared apps' results reach the model.
        self.lock = asyncio.Lock()
        self._secrets: dict | None = None
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS mcp_servers (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "kind TEXT NOT NULL, url TEXT, command TEXT, args TEXT, created REAL NOT NULL, "
                "shared INTEGER NOT NULL DEFAULT 0, prefs TEXT NOT NULL DEFAULT '{}')"
            )

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    # Stored apps --------------------------------------------------------------------------

    COLUMNS = ("id", "name", "kind", "url", "command", "args", "created", "shared", "prefs")

    def servers(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                f"SELECT {', '.join(self.COLUMNS)} FROM mcp_servers ORDER BY created"
            ).fetchall()
        items = []
        for row in rows:
            item = dict(zip(self.COLUMNS, row, strict=True))
            item["args"] = json.loads(item["args"] or "[]")
            item["prefs"] = json.loads(item["prefs"] or "{}")
            item["shared"] = bool(item["shared"])
            items.append(item)
        return items

    def server(self, server_id: str) -> dict:
        for item in self.servers():
            if item["id"] == server_id:
                return item
        raise MCPError("That app isn't connected to Bridge.")

    def find(self, name: str) -> dict:
        """An app by its id or name (as the model says it)."""
        wanted = name.strip().lower()
        for item in self.servers():
            if wanted in {item["id"], item["name"].lower(), slug(item["name"])}:
                return item
        raise MCPError(f"No app called “{name}” is connected. Connect it in Connections.")

    async def _all_secrets(self) -> dict:
        if self._secrets is None:
            self._secrets = await asyncio.to_thread(self.secrets.load)
        return self._secrets

    async def _secret(self, server_id: str) -> dict:
        return dict((await self._all_secrets()).get(server_id) or {})

    async def _save_secret(self, server_id: str, value: dict | None) -> None:
        data = dict(await self._all_secrets())
        if value is None:
            data.pop(server_id, None)
        else:
            data[server_id] = value
        await asyncio.to_thread(self.secrets.save, data)
        self._secrets = data

    async def add(
        self,
        *,
        name: str,
        kind: str,
        url: str = "",
        command: str = "",
        args: list[str] = (),
        headers: dict | None = None,
        env: dict | None = None,
        shared: bool = False,
    ) -> dict:
        if kind not in {"http", "sse", "stdio"}:
            raise MCPError("Choose a web address or a program on this Mac.")
        name = " ".join(name.split())[:40] or "App"
        if kind == "stdio":
            if not command.strip():
                raise MCPError("Enter the program to run, e.g. npx.")
        else:
            url = _check_url(url)
            if kind == "http" and urlsplit(url).path.rstrip("/").endswith("/sse"):
                kind = "sse"  # Older servers: the address ends in /sse.
        server_id = f"{slug(name)}-{uuid4().hex[:6]}"
        with self._db() as db:
            db.execute(
                "INSERT INTO mcp_servers (id, name, kind, url, command, args, created, shared) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    server_id,
                    name,
                    kind,
                    url or None,
                    command.strip() or None,
                    json.dumps(list(args)),
                    self.clock(),
                    int(shared),
                ),
            )
        await self._save_secret(server_id, {"headers": dict(headers or {}), "env": dict(env or {})})
        if shared:
            self._share(server_id, True)
        return await self.connect(server_id)

    async def remove(self, server_id: str) -> None:
        self.server(server_id)
        await self._close(server_id)
        with self._db() as db:
            db.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
        await self._save_secret(server_id, None)
        self._share(server_id, False)
        self.tools.pop(server_id, None)
        self.status.pop(server_id, None)

    def set_pref(self, server_id: str, tool: str, mode: str) -> None:
        if mode not in MODES:
            raise MCPError("Choose auto, ask or off.")
        server = self.server(server_id)
        prefs = server["prefs"]
        prefs[tool] = mode
        with self._db() as db:
            db.execute(
                "UPDATE mcp_servers SET prefs = ? WHERE id = ?", (json.dumps(prefs), server_id)
            )

    def set_shared(self, server_id: str, shared: bool) -> None:
        self.server(server_id)
        with self._db() as db:
            db.execute("UPDATE mcp_servers SET shared = ? WHERE id = ?", (int(shared), server_id))
        self._share(server_id, shared)

    def _share(self, server_id: str, shared: bool) -> None:
        if self.privacy is None:
            return
        key = f"mcp:{server_id}"
        if shared:
            self.privacy.allow([key])
        elif hasattr(self.privacy, "disallow"):
            self.privacy.disallow([key])

    def apply_sharing(self) -> None:
        """At startup: the apps you share, plus the tool list itself (names, not your data)."""
        if self.privacy is None:
            return
        self.privacy.allow(
            ["apps_find"] + [f"mcp:{s['id']}" for s in self.servers() if s["shared"]]
        )

    # Connecting ---------------------------------------------------------------------------

    async def _close(self, server_id: str) -> None:
        client = self.clients.pop(server_id, None)
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    async def _client(self, server: dict):
        secret = await self._secret(server["id"])
        if server["kind"] == "stdio":
            return StdioClient(server["command"], server["args"], secret.get("env") or {})
        headers = dict(secret.get("headers") or {})
        tokens = secret.get("oauth") or {}
        if tokens.get("access_token"):
            if tokens.get("expires_at", 0) < self.clock() + 60 and tokens.get("refresh_token"):
                tokens = await self._refresh(server, secret)
            headers["Authorization"] = f"Bearer {tokens['access_token']}"
        kind = SSEClient if server["kind"] == "sse" else HTTPClient
        return kind(server["url"], headers, self.http_factory())

    async def _refresh(self, server: dict, secret: dict) -> dict:
        tokens = secret["oauth"]
        http = self.http_factory()
        try:
            fresh = await oauth.refresh(
                http,
                tokens["token_endpoint"],
                tokens["refresh_token"],
                tokens["client"],
                server["url"],
            )
        finally:
            await http.aclose()
        tokens = {**tokens, **fresh}
        await self._save_secret(server["id"], {**secret, "oauth": tokens})
        return tokens

    async def connect(self, server_id: str) -> dict:
        """(Re)connect and learn the app's tools."""
        server = self.server(server_id)
        await self._close(server_id)
        try:
            client = await self._client(server)
            await client.initialize()
            tools = await client.list_tools()
        except NeedsSignIn as exc:
            self.status[server_id] = {
                "state": "sign_in",
                "detail": "Sign in to use this app.",
                "resource_metadata": exc.resource_metadata,
            }
            return self.view(server)
        except MCPError as exc:
            self.status[server_id] = {"state": "error", "detail": str(exc)}
            return self.view(server)
        self.clients[server_id] = client
        self.tools[server_id] = [
            {
                "name": str(t.get("name", ""))[:100],
                "title": str(
                    t.get("title") or (t.get("annotations") or {}).get("title") or t.get("name", "")
                )[:100],
                "description": " ".join(str(t.get("description") or "").split())[:600],
                "schema": t.get("inputSchema") or {"type": "object"},
                "read_only": bool((t.get("annotations") or {}).get("readOnlyHint")),
                "destructive": bool((t.get("annotations") or {}).get("destructiveHint")),
            }
            for t in tools
            if t.get("name")
        ]
        info = getattr(client, "server_info", {}) or {}
        self.status[server_id] = {
            "state": "ok",
            "detail": f"{len(self.tools[server_id])} tools",
            "server": str(info.get("name") or "")[:60],
        }
        return self.view(server)

    async def ensure(self, server: dict) -> None:
        if server["id"] not in self.clients:
            await self.connect(server["id"])
        state = self.status.get(server["id"], {}).get("state")
        if state == "sign_in":
            raise MCPError(f"Sign in to {server['name']} in Connections first.")
        if state != "ok":
            raise MCPError(self.status.get(server["id"], {}).get("detail") or "Couldn't connect.")

    def view(self, server: dict) -> dict:
        status = self.status.get(server["id"]) or {"state": "unknown", "detail": "Not checked yet."}
        return {
            "id": server["id"],
            "name": server["name"],
            "kind": server["kind"],
            "url": server["url"],
            "command": " ".join([server["command"] or "", *server["args"]]).strip(),
            "shared": server["shared"],
            "state": status["state"],
            "detail": status["detail"],
            "tools": len(self.tools.get(server["id"], [])),
        }

    # Signing in ---------------------------------------------------------------------------

    async def begin_sign_in(self, server_id: str) -> str:
        server = self.server(server_id)
        if server["kind"] == "stdio":
            raise MCPError("Programs on this Mac don't sign in this way.")
        http = self.http_factory()
        try:
            meta = await oauth.discover(
                http, server["url"], self.status.get(server_id, {}).get("resource_metadata", "")
            )
            secret = await self._secret(server_id)
            client = (secret.get("oauth") or {}).get("client")
            if not client:
                client = await oauth.register(
                    http, meta.get("registration_endpoint"), self.redirect_uri
                )
        finally:
            await http.aclose()
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(32)
        now = self.clock()
        self.pending = {k: v for k, v in self.pending.items() if v["expires"] > now}
        self.pending[state] = {
            "server": server_id,
            "verifier": verifier,
            "meta": meta,
            "client": client,
            "expires": now + 600,
        }
        return oauth.authorization_url(
            meta, client["client_id"], self.redirect_uri, state, challenge, server["url"]
        )

    async def finish_sign_in(self, state: str, code: str) -> dict:
        pending = self.pending.pop(state, None)
        if pending is None or pending["expires"] < self.clock():
            raise MCPError("This sign-in expired. Start it again from Connections.")
        server = self.server(pending["server"])
        http = self.http_factory()
        try:
            tokens = await oauth.exchange(
                http,
                pending["meta"]["token_endpoint"],
                code,
                pending["verifier"],
                pending["client"],
                self.redirect_uri,
                server["url"],
            )
        finally:
            await http.aclose()
        secret = await self._secret(server["id"])
        await self._save_secret(
            server["id"],
            {
                **secret,
                "oauth": {
                    **tokens,
                    "client": pending["client"],
                    "token_endpoint": pending["meta"]["token_endpoint"],
                },
            },
        )
        return await self.connect(server["id"])

    # Using tools ----------------------------------------------------------------------------

    def tool_info(self, server: dict, tool: str) -> dict:
        for item in self.tools.get(server["id"], []):
            if item["name"] == tool:
                return item
        raise MCPError(f"{server['name']} has no tool called “{tool}”. Use apps_find first.")

    def mode(self, server: dict, tool: str) -> str:
        return server["prefs"].get(tool, "auto")

    def asks_first(self, server: dict, tool: dict) -> bool:
        mode = self.mode(server, tool["name"])
        if mode == "ask":
            return True
        return not tool["read_only"]

    async def call(self, server: dict, tool: str, arguments: dict) -> dict:
        await self.ensure(server)
        if self.mode(server, tool) == "off":
            raise MCPError(f"You turned off “{tool}” in {server['name']}.")
        self.tool_info(server, tool)
        client = self.clients[server["id"]]
        try:
            result = await client.call_tool(tool, arguments)
        except NeedsSignIn:
            secret = await self._secret(server["id"])
            if (secret.get("oauth") or {}).get("refresh_token"):
                await self._refresh(server, secret)
                await self.connect(server["id"])
                result = await self.clients[server["id"]].call_tool(tool, arguments)
            else:
                self.status[server["id"]] = {"state": "sign_in", "detail": "Sign in again."}
                raise MCPError(f"Sign in to {server['name']} again in Connections.") from None
        except MCPError:
            await self._close(server["id"])  # Reconnect next time.
            raise
        return {"text": result_text(result), "is_error": bool(result.get("isError"))}

    async def search(self, app: str = "", query: str = "", limit: int = 40) -> list[dict]:
        servers = [self.find(app)] if app.strip() else self.servers()
        words = [w for w in query.lower().split() if len(w) > 2]
        found = []
        for server in servers:
            try:
                await self.ensure(server)
            except MCPError as exc:
                found.append({"app": server["name"], "problem": str(exc)})
                continue
            for tool in self.tools.get(server["id"], []):
                if self.mode(server, tool["name"]) == "off":
                    continue
                text = f"{tool['name']} {tool['title']} {tool['description']}".lower()
                if words and not any(w in text for w in words):
                    continue
                found.append(
                    {
                        "app": server["name"],
                        "tool": tool["name"],
                        "description": tool["description"][:300],
                        "arguments": tool["schema"],
                        "asks_first": self.asks_first(server, tool),
                    }
                )
        return found[:limit]

    async def close(self) -> None:
        for server_id in list(self.clients):
            await self._close(server_id)


def pkce_pair() -> tuple[str, str]:
    return oauth.pkce()
