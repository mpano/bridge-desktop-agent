"""Only explicit authenticated UI requests initiate account connections."""

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import Field

from app.integrations.models import IntegrationError, Provider
from app.tools.base import Input


class ConnectInput(Input):
    provider: Provider
    allow_actions: bool = False


class DisconnectInput(Input):
    account_id: str = Field(min_length=3, max_length=512)


def install_connections(app, authorize):
    def manager(request):
        accounts = getattr(request.app.state.agent, "accounts", None)
        if accounts is None:
            raise HTTPException(503, "Connected services are unavailable in this agent.")
        return accounts

    @app.get("/api/v1/connections", dependencies=[Depends(authorize)])
    async def connections(request: Request):
        try:
            return await manager(request).catalog()
        except IntegrationError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/v1/connections/start", dependencies=[Depends(authorize)])
    async def start(payload: ConnectInput, request: Request):
        try:
            return await manager(request).begin(payload.provider, payload.allow_actions)
        except IntegrationError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/v1/connections/disconnect", dependencies=[Depends(authorize)])
    async def disconnect(payload: DisconnectInput, request: Request):
        try:
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
