"""A small MCP client: JSON-RPC over Streamable HTTP, legacy HTTP+SSE, or a local program.

Only the parts Bridge needs: initialize, list tools, call a tool. Everything a server sends is
data; tool results reach the model only under Bridge's privacy rules (see manager.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from urllib.parse import urljoin, urlsplit

import httpx

PROTOCOL = "2025-06-18"
CLIENT = {"name": "Bridge", "version": "1.0"}
TIMEOUT = 60


class MCPError(RuntimeError):
    """Safe to show to the user."""


class NeedsSignIn(MCPError):
    """The server wants OAuth; `resource_metadata` says where to start, if it told us."""

    def __init__(self, resource_metadata: str = ""):
        super().__init__("Sign in to this app first.")
        self.resource_metadata = resource_metadata


def _resource_metadata(response: httpx.Response) -> str:
    header = response.headers.get("www-authenticate", "")
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key.strip().lower().endswith("resource_metadata"):
            return value.strip().strip('"')
    return ""


def _events(text: str):
    """JSON messages from a text/event-stream body."""
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")
        )
        if data:
            try:
                yield json.loads(data)
            except ValueError:
                continue


class Client:
    """Common JSON-RPC bookkeeping; transports implement _request and _notify."""

    def __init__(self):
        self._next = 0
        self.server_info: dict = {}

    def _id(self) -> int:
        self._next += 1
        return self._next

    async def _request(self, method: str, params: dict | None = None) -> dict:
        raise NotImplementedError

    async def _notify(self, method: str, params: dict | None = None) -> None:
        raise NotImplementedError

    async def initialize(self) -> dict:
        result = await self._request(
            "initialize",
            {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT},
        )
        self.server_info = result.get("serverInfo") or {}
        await self._notify("notifications/initialized")
        return result

    async def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        for _ in range(20):  # Pages, with a limit in case a server never stops.
            result = await self._request("tools/list", {"cursor": cursor} if cursor else {})
            tools += result.get("tools") or []
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self._request("tools/call", {"name": name, "arguments": arguments})

    async def close(self) -> None:
        pass

    @staticmethod
    def _result(message: dict) -> dict:
        if "error" in message:
            error = message["error"] or {}
            raise MCPError(str(error.get("message") or "The app returned an error.")[:300])
        return message.get("result") or {}


class HTTPClient(Client):
    """Streamable HTTP: each request is a POST; the answer is JSON or an event stream."""

    def __init__(
        self, url: str, headers: dict | None = None, http: httpx.AsyncClient | None = None
    ):
        super().__init__()
        self.url = url
        self.headers = dict(headers or {})
        self.http = http or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)
        self.session: str | None = None
        self._initialized = False

    def _headers(self) -> dict:
        headers = {
            **self.headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._initialized:
            headers["MCP-Protocol-Version"] = PROTOCOL
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        return headers

    async def _post(self, message: dict) -> httpx.Response:
        try:
            response = await self.http.post(self.url, json=message, headers=self._headers())
        except httpx.HTTPError:
            raise MCPError("Couldn't reach the app's server.") from None
        if response.status_code == 401:
            raise NeedsSignIn(_resource_metadata(response))
        if response.status_code == 404 and self.session:
            self.session, self._initialized = None, False
            raise MCPError("The app ended the session. Try again.")
        if response.status_code >= 400:
            raise MCPError(f"The app's server answered {response.status_code}.")
        if response.headers.get("mcp-session-id"):
            self.session = response.headers["mcp-session-id"]
        return response

    async def _request(self, method: str, params: dict | None = None) -> dict:
        number = self._id()
        response = await self._post(
            {"jsonrpc": "2.0", "id": number, "method": method, "params": params or {}}
        )
        kind = response.headers.get("content-type", "")
        if "text/event-stream" in kind:
            for message in _events(response.text):
                if message.get("id") == number:
                    if method == "initialize":
                        self._initialized = True
                    return self._result(message)
            raise MCPError("The app didn't answer.")
        try:
            message = response.json()
        except ValueError:
            raise MCPError("The app sent something Bridge couldn't read.") from None
        if method == "initialize":
            self._initialized = True
        return self._result(message)

    async def _notify(self, method: str, params: dict | None = None) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def close(self) -> None:
        await self.http.aclose()


class SSEClient(Client):
    """The older HTTP+SSE transport: a long event stream, and POSTs to the URL it names."""

    def __init__(
        self, url: str, headers: dict | None = None, http: httpx.AsyncClient | None = None
    ):
        super().__init__()
        self.url = url
        self.headers = dict(headers or {})
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT, read=None))
        self.endpoint: str | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.ready = asyncio.Event()
        self.reader: asyncio.Task | None = None
        self.failure: Exception | None = None

    async def _listen(self) -> None:
        try:
            async with self.http.stream(
                "GET", self.url, headers={**self.headers, "Accept": "text/event-stream"}
            ) as response:
                if response.status_code == 401:
                    raise NeedsSignIn(_resource_metadata(response))
                if response.status_code >= 400:
                    raise MCPError(f"The app's server answered {response.status_code}.")
                event, data = "message", []
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line.strip() and data:
                        payload = "\n".join(data)
                        data = []
                        if event == "endpoint":
                            target = urljoin(self.url, payload)
                            if urlsplit(target).netloc != urlsplit(self.url).netloc:
                                raise MCPError("The app pointed Bridge somewhere else.")
                            self.endpoint = target
                            self.ready.set()
                        else:
                            try:
                                message = json.loads(payload)
                            except ValueError:
                                continue
                            waiter = self.pending.pop(message.get("id"), None)
                            if waiter is not None and not waiter.done():
                                waiter.set_result(message)
                        event = "message"
        except Exception as exc:  # noqa: BLE001 — handed to whoever is waiting.
            self.failure = exc if isinstance(exc, MCPError) else MCPError("The app's stream ended.")
            self.ready.set()
            for waiter in self.pending.values():
                if not waiter.done():
                    waiter.set_exception(self.failure)

    async def _start(self) -> None:
        if self.reader is None:
            self.reader = asyncio.get_running_loop().create_task(self._listen())
        try:
            await asyncio.wait_for(self.ready.wait(), 20)
        except TimeoutError:
            raise MCPError("The app's server didn't answer.") from None
        if self.failure:
            raise self.failure

    async def _send(self, message: dict) -> None:
        await self._start()
        try:
            response = await self.http.post(
                self.endpoint,
                json=message,
                headers={**self.headers, "Content-Type": "application/json"},
            )
        except httpx.HTTPError:
            raise MCPError("Couldn't reach the app's server.") from None
        if response.status_code == 401:
            raise NeedsSignIn(_resource_metadata(response))
        if response.status_code >= 400:
            raise MCPError(f"The app's server answered {response.status_code}.")

    async def _request(self, method: str, params: dict | None = None) -> dict:
        number = self._id()
        waiter = asyncio.get_running_loop().create_future()
        self.pending[number] = waiter
        await self._send({"jsonrpc": "2.0", "id": number, "method": method, "params": params or {}})
        try:
            message = await asyncio.wait_for(waiter, TIMEOUT)
        except TimeoutError:
            self.pending.pop(number, None)
            raise MCPError("The app took too long to answer.") from None
        return self._result(message)

    async def _notify(self, method: str, params: dict | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def close(self) -> None:
        if self.reader is not None:
            self.reader.cancel()
        await self.http.aclose()


class StdioClient(Client):
    """A local program you added: Bridge starts it (never through a shell) and talks over pipes."""

    def __init__(self, command: str, args: list[str], env: dict | None = None):
        super().__init__()
        self.command, self.args, self.env = command, list(args), dict(env or {})
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.reader: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    async def _start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        path = (
            shutil.which(
                self.command, path=os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"
            )
            if "/" not in self.command
            else self.command
        )
        if not path:
            raise MCPError(f"Bridge can't find “{self.command}” on this Mac.")
        try:
            self.process = await asyncio.create_subprocess_exec(
                path,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, **self.env},
                limit=8 * 1024 * 1024,
            )
        except OSError:
            raise MCPError(f"Couldn't start “{self.command}”.") from None
        self.reader = asyncio.get_running_loop().create_task(self._listen())

    async def _listen(self) -> None:
        assert self.process and self.process.stdout
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue  # Programs sometimes print logs on stdout.
            waiter = (
                self.pending.pop(message.get("id"), None) if isinstance(message, dict) else None
            )
            if waiter is not None and not waiter.done():
                waiter.set_result(message)
        for waiter in self.pending.values():
            if not waiter.done():
                waiter.set_exception(MCPError("The app's program stopped."))

    async def _write(self, message: dict) -> None:
        await self._start()
        assert self.process and self.process.stdin
        async with self.lock:
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            await self.process.stdin.drain()

    async def _request(self, method: str, params: dict | None = None) -> dict:
        number = self._id()
        waiter = asyncio.get_running_loop().create_future()
        self.pending[number] = waiter
        await self._write(
            {"jsonrpc": "2.0", "id": number, "method": method, "params": params or {}}
        )
        try:
            message = await asyncio.wait_for(waiter, TIMEOUT if method != "initialize" else 120)
        except TimeoutError:
            self.pending.pop(number, None)
            raise MCPError("The app took too long to answer.") from None
        return self._result(message)

    async def _notify(self, method: str, params: dict | None = None) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def close(self) -> None:
        if self.reader is not None:
            self.reader.cancel()
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                self.process.kill()


def result_text(result: dict, limit: int = 6000) -> str:
    """A tool result as readable text (images and files are named, not inlined)."""
    parts = []
    for item in result.get("content") or []:
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "resource":
            resource = item.get("resource") or {}
            parts.append(str(resource.get("text") or resource.get("uri") or ""))
        elif kind == "resource_link":
            parts.append(f"{item.get('name') or ''} {item.get('uri') or ''}".strip())
        elif kind in {"image", "audio"}:
            parts.append(f"[{kind}]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(p for p in parts if p).strip()
    return text[:limit] + ("…" if len(text) > limit else "")
