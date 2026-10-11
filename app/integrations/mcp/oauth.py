"""Signing in to an MCP app with OAuth (the MCP authorization spec): find the app's sign-in
server, register Bridge with it, and swap the code from your browser for tokens (PKCE)."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx

from app.integrations.mcp.client import MCPError


def _secure(url: str) -> str:
    parts = urlsplit(url)
    local = parts.hostname in {"localhost", "127.0.0.1"}
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise MCPError("The app's sign-in server isn't secure (https).")
    return url


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


async def _json(http: httpx.AsyncClient, url: str) -> dict | None:
    try:
        response = await http.get(_secure(url), headers={"Accept": "application/json"})
    except (httpx.HTTPError, MCPError):
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def discover(http: httpx.AsyncClient, server_url: str, resource_metadata: str = "") -> dict:
    """Where to sign in for this app: its authorization server's endpoints."""
    parts = urlsplit(server_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    resource = None
    for url in filter(
        None,
        [
            resource_metadata,
            f"{origin}/.well-known/oauth-protected-resource{path}",
            f"{origin}/.well-known/oauth-protected-resource",
        ],
    ):
        resource = await _json(http, url)
        if resource:
            break
    issuers = (resource or {}).get("authorization_servers") or [origin]
    issuer = str(issuers[0]).rstrip("/")
    split = urlsplit(issuer)
    base, issuer_path = f"{split.scheme}://{split.netloc}", split.path.rstrip("/")
    meta = None
    for url in [
        f"{base}/.well-known/oauth-authorization-server{issuer_path}",
        f"{base}/.well-known/openid-configuration{issuer_path}",
        f"{issuer}/.well-known/openid-configuration",
        f"{base}/.well-known/oauth-authorization-server",
    ]:
        meta = await _json(http, url)
        if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
            break
    if not meta or not meta.get("authorization_endpoint"):
        raise MCPError("This app didn't say how to sign in. It may need an API key instead.")
    return {
        "authorization_endpoint": _secure(meta["authorization_endpoint"]),
        "token_endpoint": _secure(meta["token_endpoint"]),
        "registration_endpoint": meta.get("registration_endpoint"),
        "scopes": (resource or {}).get("scopes_supported") or [],
    }


async def register(http: httpx.AsyncClient, endpoint: str | None, redirect_uri: str) -> dict:
    """Dynamic client registration: Bridge introduces itself to the app's sign-in server."""
    if not endpoint:
        raise MCPError("This app needs Bridge registered by hand; use an API key instead.")
    try:
        response = await http.post(
            _secure(endpoint),
            json={
                "client_name": "Bridge",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
    except httpx.HTTPError:
        raise MCPError("Couldn't reach the app's sign-in server.") from None
    if response.status_code >= 400:
        raise MCPError("The app's sign-in server didn't accept Bridge.")
    data = response.json()
    if not data.get("client_id"):
        raise MCPError("The app's sign-in server didn't accept Bridge.")
    return {"client_id": data["client_id"], "client_secret": data.get("client_secret")}


def authorization_url(
    meta: dict, client_id: str, redirect_uri: str, state: str, challenge: str, resource: str
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource,
    }
    if meta.get("scopes"):
        params["scope"] = " ".join(meta["scopes"])
    return (
        meta["authorization_endpoint"]
        + ("&" if "?" in meta["authorization_endpoint"] else "?")
        + urlencode(params)
    )


async def _token(http: httpx.AsyncClient, endpoint: str, form: dict) -> dict:
    try:
        response = await http.post(
            _secure(endpoint), data=form, headers={"Accept": "application/json"}
        )
    except httpx.HTTPError:
        raise MCPError("Couldn't reach the app's sign-in server.") from None
    if response.status_code >= 400:
        raise MCPError("The app's sign-in didn't finish. Try signing in again.")
    data = response.json()
    if not data.get("access_token"):
        raise MCPError("The app's sign-in didn't finish. Try signing in again.")
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at": time.time() + float(data.get("expires_in") or 3600),
    }


async def exchange(http, endpoint, code, verifier, client, redirect_uri, resource) -> dict:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "resource": resource,
    }
    if client.get("client_secret"):
        form["client_secret"] = client["client_secret"]
    return await _token(http, endpoint, form)


async def refresh(http, endpoint, token, client, resource) -> dict:
    form = {
        "grant_type": "refresh_token",
        "refresh_token": token,
        "client_id": client["client_id"],
        "resource": resource,
    }
    if client.get("client_secret"):
        form["client_secret"] = client["client_secret"]
    fresh = await _token(http, endpoint, form)
    fresh["refresh_token"] = fresh.get("refresh_token") or token
    return fresh
