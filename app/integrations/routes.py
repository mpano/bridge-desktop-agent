"""Only explicit authenticated UI requests initiate account connections."""

import asyncio
import json
from typing import Literal

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import Field

from app.integrations.models import IntegrationError, Provider
from app.integrations.tokens import TOKEN_PROVIDERS
from app.tools.base import Input
from app.tools.work.tools import GITHUB_READS, JIRA_READS

# A sign of "share results from my services" being on (Settings › Privacy).
SHARING_SIGNS = ("email_search", "slack_search", "calendar_events")


class ConnectInput(Input):
    provider: Provider
    allow_actions: bool = False


class DisconnectInput(Input):
    account_id: str = Field(min_length=3, max_length=512)


class TokenConnectInput(Input):
    provider: Literal["jira", "github"]
    site: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=254)
    token: str = Field(default="", max_length=500)
    use_cli: bool = False
    allow_actions: bool = True


def install_connections(app, authorize, settings=None):
    def manager(request):
        accounts = getattr(request.app.state.agent, "accounts", None)
        if accounts is None:
            raise HTTPException(503, "Connected services are unavailable in this agent.")
        return accounts

    def tokens(request):
        return getattr(request.app.state.agent, "tokens", None)

    async def share_reads(request, provider: str) -> bool:
        """You share results from your services: a newly connected one is shared the same way."""
        if settings is None or settings.remote_tool_results != "allowlist":
            return False
        allowed = list(settings.remote_tool_result_allowlist)
        if not any(name in allowed for name in SHARING_SIGNS):
            return False
        names = [
            n for n in (JIRA_READS if provider == "jira" else GITHUB_READS) if n not in allowed
        ]
        if not names:
            return True
        from app.api.app_settings import env_path
        from app.config.env_file import update_env

        allowed += names
        await asyncio.to_thread(
            update_env, env_path(), {"REMOTE_TOOL_RESULT_ALLOWLIST": json.dumps(allowed)}
        )
        settings.remote_tool_result_allowlist = allowed
        planner = getattr(request.app.state.agent, "planner", None)
        if planner is not None and hasattr(planner.privacy, "allow"):
            planner.privacy.allow(names)
        return True

    @app.get("/api/v1/connections", dependencies=[Depends(authorize)])
    async def connections(request: Request):
        try:
            data = await manager(request).catalog()
        except IntegrationError as exc:
            raise HTTPException(503, str(exc)) from None
        work = tokens(request)
        if work is not None:
            try:
                listed = await work.catalog()
            except IntegrationError as exc:
                listed = [
                    {
                        "provider": key,
                        "name": info["name"],
                        "kind": "token",
                        "configured": True,
                        "account": None,
                        "error": str(exc),
                    }
                    for key, info in TOKEN_PROVIDERS.items()
                ]
            for item in listed:
                account = item.pop("account")
                data["providers"].append({**item, "read_scopes": [], "write_scopes": ["actions"]})
                if account:
                    scopes = ["read"] + (["actions"] if account["actions"] else [])
                    data.setdefault("accounts", []).append({**account, "scopes": scopes})
        return data

    @app.post("/api/v1/connections/token", dependencies=[Depends(authorize)])
    async def connect_token(payload: TokenConnectInput, request: Request):
        work = tokens(request)
        if work is None:
            raise HTTPException(503, "Work connections are unavailable.")
        try:
            result = await work.connect(
                payload.provider,
                site=payload.site,
                email=payload.email,
                token=payload.token,
                use_cli=payload.use_cli,
                actions=payload.allow_actions,
            )
        except IntegrationError as exc:
            raise HTTPException(400, str(exc)) from None
        return {**result, "shared": await share_reads(request, payload.provider)}

    @app.post("/api/v1/connections/start", dependencies=[Depends(authorize)])
    async def start(payload: ConnectInput, request: Request):
        try:
            return await manager(request).begin(payload.provider, payload.allow_actions)
        except IntegrationError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/v1/connections/disconnect", dependencies=[Depends(authorize)])
    async def disconnect(payload: DisconnectInput, request: Request):
        try:
            if payload.account_id in TOKEN_PROVIDERS and tokens(request) is not None:
                return await tokens(request).disconnect(payload.account_id)
            return await manager(request).disconnect(payload.account_id)
        except IntegrationError as exc:
            raise HTTPException(400, str(exc)) from None

    # Provider redirects cannot send our API bearer token. A short-lived, single-use
    # state bound to the provider plus PKCE authorizes ONLY this token exchange.
    @app.get("/api/v1/connections/callback/{provider}", include_in_schema=False)
    async def callback(
        provider: Provider,
        request: Request,
        state: str = Query(max_length=200),
        code: str | None = Query(default=None, max_length=4096),
        error: str | None = Query(default=None, max_length=200),
    ):
        try:
            accounts = manager(request)
            if str(request.url).split("?", 1)[0] != accounts.redirect_uri(provider):
                raise IntegrationError(
                    "OAuth callback address does not match the configured local address."
                )
            await accounts.finish(provider, state, code, error)
            return PlainTextResponse(
                "Account connected to Bridge. Close this tab and refresh Connections. No action was"
                " sent."
            )
        except IntegrationError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        except Exception:
            return PlainTextResponse(
                "Connection could not be completed. Restart it from Connections; no credentials are"
                " displayed here.",
                status_code=400,
            )
