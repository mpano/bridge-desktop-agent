"""MCP apps: connecting, signing in, finding and using tools safely."""

import asyncio
import json
import sys
import textwrap
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.integrations.mcp.client import StdioClient
from app.integrations.mcp.manager import MCPManager
from app.integrations.tokens import MemoryTokens
from app.security.risk import RiskLevel

URL = "https://mcp.example.test/mcp"
REDIRECT = "http://127.0.0.1:8000/api/v1/mcp/callback"
TOOLS = [
    {
        "name": "search",
        "description": "Search pages",
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    },
    {"name": "create_page", "description": "Create a page", "inputSchema": {"type": "object"}},
]


class App:
    """A pretend MCP app (Streamable HTTP), optionally behind OAuth."""

    def __init__(self, oauth=False):
        self.oauth = oauth
        self.seen = []
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        url = str(request.url)
        if url.startswith("https://mcp.example.test/.well-known/oauth-protected-resource"):
            return httpx.Response(
                200, json={"authorization_servers": ["https://auth.example.test"]}
            )
        if url == "https://auth.example.test/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                    "registration_endpoint": "https://auth.example.test/register",
                },
            )
        if url == "https://auth.example.test/register":
            assert json.loads(request.content)["redirect_uris"] == [REDIRECT]
            return httpx.Response(201, json={"client_id": "bridge-client"})
        if url == "https://auth.example.test/token":
            form = parse_qs(request.content.decode())
            assert form["code"] == ["the-code"] and form["code_verifier"][0]
            return httpx.Response(
                200, json={"access_token": "tok", "refresh_token": "ref", "expires_in": 3600}
            )
        if url.startswith("https://mcp.example.test/"):
            if self.oauth and request.headers.get("authorization") != "Bearer tok":
                return httpx.Response(
                    401,
                    headers={
                        "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource"'
                    },
                )
            message = json.loads(request.content)
            if "id" not in message:
                return httpx.Response(202)
            method = message["method"]
            if method == "initialize":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"serverInfo": {"name": "Pages"}},
                    },
                    headers={"Mcp-Session-Id": "s-1"},
                )
            assert request.headers["mcp-session-id"] == "s-1"
            if method == "tools/list":
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": message["id"], "result": {"tools": TOOLS}}
                )
            if method == "tools/call":
                self.calls.append(message["params"])
                body = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"content": [{"type": "text", "text": "Found: Q3 plan"}]},
                }
                # Answered as an event stream, as many servers do.
                return httpx.Response(
                    200,
                    text=f"event: message\ndata: {json.dumps(body)}\n\n",
                    headers={"content-type": "text/event-stream"},
                )
        return httpx.Response(404)


def manager(tmp_path, app):
    return MCPManager(
        tmp_path / "db",
        REDIRECT,
        secrets_store=MemoryTokens(),
        http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(app)),
    )


def test_connect_list_and_call_over_http(tmp_path):
    app = App()
    m = manager(tmp_path, app)

    async def scenario():
        view = await m.add(name="Pages", kind="http", url=URL)
        server = m.find("pages")
        found = await m.search(query="search")
        result = await m.call(server, "search", {"query": "Q3"})
        return view, server, found, result

    view, server, found, result = asyncio.run(scenario())
    assert view["state"] == "ok" and view["tools"] == 2
    assert found == [
        {
            "app": "Pages",
            "tool": "search",
            "description": "Search pages",
            "arguments": {"type": "object"},
            "asks_first": False,
        }
    ]
    assert result == {"text": "Found: Q3 plan", "is_error": False}
    assert app.calls == [{"name": "search", "arguments": {"query": "Q3"}}]
    assert m.asks_first(server, m.tool_info(server, "create_page"))  # Not read-only: asks.
    m.set_pref(server["id"], "search", "ask")
    assert m.asks_first(m.server(server["id"]), m.tool_info(server, "search"))
    m.set_pref(server["id"], "search", "off")
    with pytest.raises(Exception, match="turned off"):
        asyncio.run(m.call(m.server(server["id"]), "search", {}))


def test_oauth_sign_in_with_pkce(tmp_path):
    app = App(oauth=True)
    m = manager(tmp_path, app)

    async def scenario():
        view = await m.add(name="Pages", kind="http", url=URL)
        url = await m.begin_sign_in(view["id"])
        state = parse_qs(urlsplit(url).query)["state"][0]
        done = await m.finish_sign_in(state, "the-code")
        again = None
        try:
            await m.finish_sign_in(state, "the-code")
        except Exception as exc:
            again = str(exc)
        return view, url, done, again

    view, url, done, again = asyncio.run(scenario())
    assert view["state"] == "sign_in"
    query = parse_qs(urlsplit(url).query)
    assert url.startswith("https://auth.example.test/authorize?")
    assert query["client_id"] == ["bridge-client"] and query["code_challenge_method"] == ["S256"]
    assert query["resource"] == [URL] and query["redirect_uri"] == [REDIRECT]
    assert done["state"] == "ok" and done["tools"] == 2
    assert "expired" in again  # A state works once.
    secret = m._secrets[view["id"]]["oauth"]
    assert secret["access_token"] == "tok" and "tok" not in json.dumps(done)


def test_a_local_program_over_pipes(tmp_path):
    server = tmp_path / "server.py"
    server.write_text(
        textwrap.dedent("""
        import json, sys
        for line in sys.stdin:
            msg = json.loads(line)
            if "id" not in msg:
                continue
            if msg["method"] == "initialize":
                result = {"serverInfo": {"name": "local"}}
            elif msg["method"] == "tools/list":
                result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
            else:
                said = msg["params"]["arguments"]["text"]
                result = {"content": [{"type": "text", "text": "echo " + said}]}
            print("a log line, not JSON", flush=True)
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
    """)
    )

    async def scenario():
        client = StdioClient(sys.executable, [str(server)])
        try:
            await client.initialize()
            tools = await client.list_tools()
            result = await client.call_tool("echo", {"text": "hi"})
        finally:
            await client.close()
        return tools, result

    tools, result = asyncio.run(scenario())
    assert tools[0]["name"] == "echo" and result["content"][0]["text"] == "echo hi"


def test_only_secure_addresses():
    m = MCPManager(":memory:", REDIRECT, secrets_store=MemoryTokens())
    with pytest.raises(Exception, match="https"):
        asyncio.run(m.add(name="x", kind="http", url="http://evil.example/mcp"))


def test_apps_tools_ask_first_unless_read_only_and_follow_sharing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db",
        api_token="token",
        remote_tool_results="allowlist",
        remote_tool_result_allowlist=["email_search"],
    )
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = agent.proactive = None
    app = App()
    agent.mcp.http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(app))
    headers = {"Authorization": "Bearer token"}
    registry = agent.executor.registry
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        added = client.post(
            "/api/v1/mcp/add", json={"name": "Pages", "url": URL}, headers=headers
        ).json()
        assert added["state"] == "ok" and added["shared"] is True  # You share service results.
        listed = client.get("/api/v1/mcp", headers=headers).json()
        assert listed["servers"][0]["tools"] == 2 and any(
            p["id"] == "notion" for p in listed["presets"]
        )
        use = registry.get("apps_use")
        read = use.validate({"app": "Pages", "tool": "search", "arguments": {}})
        write = use.validate({"app": "Pages", "tool": "create_page", "arguments": {}})
        assert use.classify(read) == RiskLevel.SAFE and use.classify(write) == RiskLevel.CONFIRM
        privacy = agent.planner.privacy
        shown = {"tool": "apps_use", "privacy_key": f"mcp:{added['id']}", "result": {"result": "x"}}
        assert "result" in privacy.filter_result(shown)
        client.post("/api/v1/mcp/share", json={"id": added["id"], "shared": False}, headers=headers)
        assert privacy.filter_result(shown)["result_withheld"] is True
        tools = client.get(f"/api/v1/mcp/tools?id={added['id']}", headers=headers).json()["tools"]
        assert [t["mode"] for t in tools] == ["auto", "auto"]
        bad = client.get("/api/v1/mcp/callback?state=nope&code=x")
        assert bad.status_code == 400 and "expired" in bad.text
        assert (
            client.post("/api/v1/mcp/remove", json={"id": added["id"]}, headers=headers).status_code
            == 200
        )
        assert client.get("/api/v1/mcp", headers=headers).json()["servers"] == []
