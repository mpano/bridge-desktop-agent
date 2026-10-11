"""Routes for your MCP apps (Connections › More apps): presets, add, sign in, tools, remove.

Only from this Mac. The sign-in callback carries no Bridge session (it's your browser coming
back from the app); a single-use, ten-minute state ties it to the sign-in you started.
"""

from __future__ import annotations

from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from app.integrations.mcp.client import MCPError
from app.integrations.mcp.manager import PRESETS

ID = Field(min_length=3, max_length=60, pattern=r"^[a-z0-9-]+$")


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Add(Body):
    name: str = Field(min_length=1, max_length=40)
    kind: Literal["http", "sse", "stdio"] = "http"
    url: str = Field(default="", max_length=500)
    command: str = Field(default="", max_length=200)
    args: list[str] = Field(default_factory=list, max_length=30)
    headers: dict[str, str] = Field(default_factory=dict, max_length=10)
    env: dict[str, str] = Field(default_factory=dict, max_length=20)


class Ref(Body):
    id: str = ID


class Pref(Ref):
    tool: str = Field(min_length=1, max_length=100)
    mode: Literal["auto", "ask", "off"]


class Share(Ref):
    shared: bool


DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>Bridge</title>
<body style="font:16px -apple-system,sans-serif;padding:48px;text-align:center">
<h2>{title}</h2><p>{text}</p></body>"""


def install(app: FastAPI, authorize, settings) -> None:
    def mcp(request: Request):
        found = getattr(request.app.state.agent, "mcp", None)
        if found is None:
            raise HTTPException(404, "Apps aren't available.")
        return found

    def shares_services() -> bool:
        """You share results from your services: a new app is shared the same way."""
        if settings.remote_tool_results == "all":
            return True
        allowed = settings.remote_tool_result_allowlist
        return settings.remote_tool_results == "allowlist" and any(
            name in allowed for name in ("email_search", "slack_search", "calendar_events")
        )

    @app.get("/api/v1/mcp", dependencies=[Depends(authorize)])
    async def listing(request: Request):
        manager = mcp(request)
        servers = [manager.view(s) for s in manager.servers()]
        added = {s["url"] for s in servers}
        return {
            "servers": servers,
            "presets": [{**p, "added": p["url"] in added} for p in PRESETS],
        }

    @app.post("/api/v1/mcp/add", dependencies=[Depends(authorize)])
    async def add(payload: Add, request: Request):
        try:
            return await mcp(request).add(
                name=payload.name,
                kind=payload.kind,
                url=payload.url,
                command=payload.command,
                args=payload.args,
                headers=payload.headers,
                env=payload.env,
                shared=shares_services(),
            )
        except MCPError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/v1/mcp/connect", dependencies=[Depends(authorize)])
    async def connect(payload: Ref, request: Request):
        try:
            return await mcp(request).connect(payload.id)
        except MCPError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/v1/mcp/signin", dependencies=[Depends(authorize)])
    async def signin(payload: Ref, request: Request):
        try:
            return {"url": await mcp(request).begin_sign_in(payload.id)}
        except MCPError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/v1/mcp/callback", include_in_schema=False)
    async def callback(
        request: Request,
        state: str = Query(default="", max_length=200),
        code: str = Query(default="", max_length=4096),
        error: str = Query(default="", max_length=200),
    ):
        if error or not code or not state:
            return HTMLResponse(
                DONE_PAGE.format(title="Sign-in cancelled", text="You can close this tab."),
                status_code=400,
            )
        try:
            view = await mcp(request).finish_sign_in(state, code)
        except MCPError as exc:
            return HTMLResponse(
                DONE_PAGE.format(title="That didn't work", text=str(exc)), status_code=400
            )
        name = view["name"]
        return HTMLResponse(
            DONE_PAGE.format(
                title=f"{name} is connected", text="You can close this tab and go back to Bridge."
            )
        )

    @app.get("/api/v1/mcp/tools", dependencies=[Depends(authorize)])
    async def tools(request: Request, id: str = Query(min_length=3, max_length=60)):
        manager = mcp(request)
        try:
            server = manager.server(id)
            await manager.ensure(server)
        except MCPError as exc:
            raise HTTPException(409, str(exc)) from None
        return {
            "tools": [
                {
                    "name": t["name"],
                    "title": t["title"],
                    "description": t["description"],
                    "read_only": t["read_only"],
                    "mode": manager.mode(server, t["name"]),
                }
                for t in manager.tools.get(id, [])
            ]
        }

    @app.post("/api/v1/mcp/pref", dependencies=[Depends(authorize)])
    async def pref(payload: Pref, request: Request):
        try:
            mcp(request).set_pref(payload.id, payload.tool, payload.mode)
        except MCPError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"saved": True}

    @app.post("/api/v1/mcp/share", dependencies=[Depends(authorize)])
    async def share(payload: Share, request: Request):
        try:
            mcp(request).set_shared(payload.id, payload.shared)
        except MCPError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"shared": payload.shared}

    @app.post("/api/v1/mcp/remove", dependencies=[Depends(authorize)])
    async def remove(payload: Ref, request: Request):
        try:
            await mcp(request).remove(payload.id)
        except MCPError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"removed": True}
