"""Tailscale on this Mac: is it ready for the phone, and Bridge's private https address.

`tailscale serve` passes https://<this-mac>.<tailnet>.ts.net to Bridge on 127.0.0.1. Only
devices signed in to your tailnet can reach it; nothing is opened to the internet. Tailscale
tells Bridge who is asking (the Tailscale-User-Login header) and removes any fake one.
"""

from __future__ import annotations

import asyncio
import json
import os

CANDIDATES = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/opt/homebrew/bin/tailscale",
    "/usr/local/bin/tailscale",
)
ADMIN_DNS = "https://login.tailscale.com/admin/dns"
PHONE_OS = {"iOS", "android"}


class TailscaleError(RuntimeError):
    """Safe to show to the user."""


def cli() -> str | None:
    return next((path for path in CANDIDATES if os.access(path, os.X_OK)), None)


async def run(*args: str, timeout: float = 20) -> str:
    path = cli()
    if path is None:
        raise TailscaleError("Install Tailscale on this Mac first.")
    # Tailscale's Mac app acts as its command line only when it sees a terminal type; from an
    # app opened in Finder (like Bridge) there is none, and it tries to start its window.
    env = {**os.environ, "TERM": os.environ.get("TERM") or "dumb"}
    process = await asyncio.create_subprocess_exec(
        path,
        *args,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        raise TailscaleError(
            "Tailscale didn't answer. Open the Tailscale app and try again."
        ) from None
    text = out.decode(errors="replace")
    if "GUI failed to start" in text:
        raise TailscaleError("Bridge couldn't use Tailscale's command line. Try again.")
    if process.returncode != 0:
        detail = (err or out).decode(errors="replace").strip().splitlines()
        raise TailscaleError(detail[-1][:300] if detail else "Tailscale reported an error.")
    return text


def parse_status(data: dict) -> dict:
    me = data.get("Self") or {}
    users = data.get("User") or {}
    login = (users.get(str(me.get("UserID"))) or {}).get("LoginName", "")
    phones = [
        {
            # iPhones call themselves "localhost"; the tailnet name is the one you see.
            "name": str(peer.get("DNSName") or "").split(".")[0] or peer.get("HostName") or "Phone",
            "os": peer.get("OS"),
            "online": bool(peer.get("Online")),
        }
        for peer in (data.get("Peer") or {}).values()
        if peer.get("OS") in PHONE_OS
    ]
    return {
        "installed": True,
        "running": data.get("BackendState") == "Running",
        "host": str(me.get("DNSName") or "").rstrip(".").lower(),
        "https": bool(data.get("CertDomains")),
        "login": login,
        "phones": phones,
    }


async def status() -> dict:
    if cli() is None:
        return {"installed": False, "running": False, "host": "", "https": False, "phones": []}
    try:
        return parse_status(json.loads(await run("status", "--json")))
    except (TailscaleError, ValueError):
        return {"installed": True, "running": False, "host": "", "https": False, "phones": []}


def serving(config: dict, host: str, port: int) -> str:
    """ "ours", "none", or "other" (something else already uses https on this Mac)."""
    web = (config.get("Web") or {}).get(f"{host}:443") or {}
    handler = (web.get("Handlers") or {}).get("/") or {}
    if handler.get("Proxy") in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
        return "ours"
    if web or "443" in (config.get("TCP") or {}):
        return "other"
    return "none"


async def serve_state(host: str, port: int) -> str:
    try:
        text = await run("serve", "status", "--json")
    except TailscaleError:
        return "none"
    try:
        return serving(json.loads(text or "{}"), host, port)
    except ValueError:
        return "none"


async def serve_on(port: int) -> None:
    await run("serve", "--bg", "--yes", "--https=443", f"http://127.0.0.1:{port}", timeout=40)


async def serve_off() -> None:
    await run("serve", "--https=443", "off")
