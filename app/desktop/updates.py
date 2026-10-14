"""Keeping the Bridge app up to date.

Once a day (and when you press "Check for updates") Bridge reads a small latest.json from
the project's GitHub releases. When there's a newer version it downloads the .zip, and
installs it only if every check passes:

* the file's SHA-256 matches the one in latest.json;
* Apple's code signature is intact, made with a Developer ID by the same team that signed
  the Bridge you're running (a stranger can't push an update, even through the release page);
* Gatekeeper accepts it (Apple notarized it);
* it's really Bridge (bundle id) and really the version announced.

Then Bridge quits, swaps the app (the old one goes to the Trash) and opens the new one. Only
the self-contained Bridge.app updates itself; a copy run from the code checkout updates
with git.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.desktop.bundle import BUNDLE_ID, VERSION

FEED = "https://github.com/mpano/bridge-desktop-agent/releases/latest/download/latest.json"
HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
CHECK_EVERY = 24 * 3600
MAX_DOWNLOAD = 600_000_000
CACHE = Path.home() / "Library/Caches/Bridge/updates"
RESTART = None  # Set by the menu bar app: quits Bridge cleanly (main thread).

# Run by /bin/sh once Bridge has quit: $1 pid, $2 the app, $3 the new app, $4 where the old
# one goes. Positional arguments only, so no path is ever read as shell code.
SWAP = r"""
while kill -0 "$1" 2>/dev/null; do sleep 0.3; done
if mv "$2" "$4"; then
  if mv "$3" "$2"; then mv "$4" "$HOME/.Trash/" 2>/dev/null; else mv "$4" "$2"; fi
fi
open "$2"
"""


UNSIGNED = (
    "This copy of Bridge isn't signed with a Developer ID, so it can't check updates. "
    "Download the new version from the release page instead."
)


class UpdateError(ValueError):
    """Safe to show to the user."""


def version_tuple(text: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(text).split("-")[0])
    if not parts:
        raise UpdateError("That release has no version number.")
    return tuple(int(p) for p in parts[:4])


def newer(latest: str, current: str) -> bool:
    return version_tuple(latest) > version_tuple(current)


def safe_url(url: str) -> str:
    parts = urlsplit(str(url))
    if parts.scheme != "https" or parts.hostname not in HOSTS:
        raise UpdateError("The update isn't on Bridge's release page, so it was ignored.")
    return str(url)


def read_manifest(data: dict) -> dict:
    """latest.json, checked: {"version", "url", "sha256", "size", "notes"}."""
    if not isinstance(data, dict):
        raise UpdateError("The release information is unreadable.")
    version, url, digest = data.get("version"), data.get("url"), str(data.get("sha256", ""))
    if not version or not url or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise UpdateError("The release information is incomplete.")
    version_tuple(version)
    return {
        "version": str(version)[:20],
        "url": safe_url(url),
        "sha256": digest,
        "size": int(data.get("size") or 0),
        "notes": " ".join(str(data.get("notes") or "").split())[:600],
        "published": str(data.get("published") or "")[:40],
    }


def running_bundle() -> Path | None:
    """The Bridge.app this process runs from, when it's the self-contained app."""
    if not getattr(sys, "frozen", False):
        return None
    app = Path(sys.executable).resolve().parents[2]
    return app if app.suffix == ".app" else None


def team_of(app: Path, run=subprocess.run) -> str:
    """The Developer ID team that signed an app ("" for ad-hoc or local signing)."""
    result = run(
        ["/usr/bin/codesign", "-dv", "--verbose=2", str(app)],
        capture_output=True,
        text=True,
        check=False,
    )
    text = (result.stdout or "") + (result.stderr or "")
    found = re.search(r"^TeamIdentifier=([A-Z0-9]{10})$", text, re.M)
    if found is None or "Authority=Developer ID Application" not in text:
        return ""
    return found.group(1)


def verify(app: Path, version: str, team: str, run=subprocess.run) -> None:
    """Every check a new Bridge.app must pass before it may replace this one."""
    if not team:
        raise UpdateError(UNSIGNED)
    info_file = app / "Contents/Info.plist"
    try:
        info = plistlib.loads(info_file.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        raise UpdateError("The download isn't a Bridge app.") from None
    if info.get("CFBundleIdentifier") != BUNDLE_ID:
        raise UpdateError("The download isn't Bridge.")
    if info.get("CFBundleShortVersionString") != version:
        raise UpdateError("The download isn't the version announced.")
    signature = run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        capture_output=True,
        text=True,
        check=False,
    )
    if signature.returncode != 0:
        raise UpdateError("The download's signature is broken.")
    if team_of(app, run) != team:
        raise UpdateError("The download wasn't signed by Bridge's developer.")
    gatekeeper = run(
        ["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)],
        capture_output=True,
        text=True,
        check=False,
    )
    if gatekeeper.returncode != 0:
        raise UpdateError("Apple hasn't approved (notarized) this download.")


class Updates:
    def __init__(
        self,
        settings,
        *,
        current: str = VERSION,
        bundle: Path | None | bool = True,
        http: httpx.AsyncClient | None = None,
        run=subprocess.run,
        spawn=subprocess.Popen,
        cache: Path = CACHE,
        clock=time.time,
    ):
        self.settings, self.current = settings, current
        self.bundle = running_bundle() if bundle is True else (bundle or None)
        self.http, self.run_command, self.spawn = http, run, spawn
        self.cache, self.clock = Path(cache), clock
        self.notify = None
        self.restart = None  # Set by the menu bar app: quits Bridge cleanly.
        self.state = {
            "state": "idle",
            "latest": "",
            "notes": "",
            "error": "",
            "checked_at": 0.0,
            "progress": 0.0,
        }
        self._manifest: dict | None = None
        self._ready: Path | None = None
        self._told: str = ""
        self._lock = asyncio.Lock()

    @property
    def supported(self) -> bool:
        return self.bundle is not None

    def overview(self) -> dict:
        return {
            **self.state,
            "current": self.current,
            "supported": self.supported,
            "automatic": bool(getattr(self.settings, "updates_automatic", True)),
            "page": "https://github.com/mpano/bridge-desktop-agent/releases",
        }

    def _set(self, **changes) -> None:
        self.state.update(changes)

    def _client(self) -> httpx.AsyncClient:
        return self.http or httpx.AsyncClient(timeout=30, follow_redirects=True)

    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        response = await client.get(safe_url(url), headers={"User-Agent": f"Bridge/{self.current}"})
        safe_url(str(response.url))  # Redirects stay on GitHub too.
        return response

    # Checking ---------------------------------------------------------------------------------

    async def check(self) -> dict:
        async with self._lock:
            return await self._check()

    async def _check(self) -> dict:
        self._set(state="checking", error="")
        feed = getattr(self.settings, "update_feed", "") or FEED
        client = self._client()
        try:
            response = await self._get(client, feed)
            if response.status_code == 404:
                self._set(state="idle", latest="", checked_at=self.clock())
                return self.overview()  # Nothing released yet.
            if response.status_code >= 400:
                raise UpdateError(f"GitHub answered {response.status_code}. Try again later.")
            manifest = read_manifest(response.json())
        except UpdateError as exc:
            self._set(state="error", error=str(exc), checked_at=self.clock())
            return self.overview()
        except (httpx.HTTPError, ValueError):
            self._set(state="error", error="Couldn't reach GitHub. Are you online?")
            return self.overview()
        finally:
            if self.http is None:
                await client.aclose()
        self._set(checked_at=self.clock(), latest=manifest["version"], notes=manifest["notes"])
        if not newer(manifest["version"], self.current):
            self._set(state="current")
            return self.overview()
        self._manifest = manifest
        if self._ready is not None and self._ready.exists():
            self._set(state="ready")
        else:
            self._set(state="available")
        return self.overview()

    # Downloading ------------------------------------------------------------------------------

    async def download(self) -> dict:
        async with self._lock:
            if not self.supported:
                raise UpdateError("This copy of Bridge runs from its code. Update it with git.")
            if self._manifest is None:
                await self._check()
            manifest = self._manifest
            if manifest is None or self.state["state"] not in {"available", "ready", "error"}:
                return self.overview()
            if self.state["state"] == "ready":
                return self.overview()
            try:
                self._ready = await self._fetch(manifest)
            except UpdateError as exc:
                self._set(state="available", error=str(exc), progress=0.0)
                raise
            self._set(state="ready", progress=1.0, error="")
            return self.overview()

    async def _fetch(self, manifest: dict) -> Path:
        team = await asyncio.to_thread(team_of, self.bundle, self.run_command)
        if not team:
            raise UpdateError(UNSIGNED)
        self.cache.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="update-", dir=self.cache))
        archive = folder / "Bridge.zip"
        digest = hashlib.sha256()
        got = 0
        self._set(state="downloading", progress=0.0, error="")
        client = self._client()
        try:
            async with client.stream(
                "GET", manifest["url"], headers={"User-Agent": f"Bridge/{self.current}"}
            ) as response:
                safe_url(str(response.url))
                if response.status_code >= 400:
                    raise UpdateError(f"GitHub answered {response.status_code}.")
                with archive.open("wb") as out:
                    async for chunk in response.aiter_bytes():
                        got += len(chunk)
                        if got > MAX_DOWNLOAD:
                            raise UpdateError("The download is far too big.")
                        digest.update(chunk)
                        out.write(chunk)
                        if manifest["size"]:
                            self._set(progress=min(got / manifest["size"], 0.99))
        except httpx.HTTPError:
            shutil.rmtree(folder, ignore_errors=True)
            raise UpdateError("The download stopped. Try again.") from None
        except UpdateError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        finally:
            if self.http is None:
                await client.aclose()
        if digest.hexdigest() != manifest["sha256"]:
            shutil.rmtree(folder, ignore_errors=True)
            raise UpdateError("The download doesn't match the release. It was thrown away.")
        unpacked = folder / "app"
        result = await asyncio.to_thread(
            self.run_command,
            ["/usr/bin/ditto", "-x", "-k", str(archive), str(unpacked)],
            capture_output=True,
            text=True,
            check=False,
        )
        app = unpacked / "Bridge.app"
        if result.returncode != 0 or not app.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
            raise UpdateError("The download couldn't be unpacked.")
        try:
            await asyncio.to_thread(verify, app, manifest["version"], team, self.run_command)
        except UpdateError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        archive.unlink(missing_ok=True)
        return app

    # Installing -------------------------------------------------------------------------------

    def install(self) -> dict:
        """Swap in the downloaded app as soon as Bridge has quit, then open it."""
        if self.state["state"] != "ready" or self._ready is None or not self._ready.is_dir():
            raise UpdateError("Download the update first.")
        restart = self.restart or RESTART
        if restart is None:
            raise UpdateError("Restart Bridge from its menu bar app to update.")
        target = self.bundle
        if not os.access(target.parent, os.W_OK):
            raise UpdateError(
                f"Bridge can't replace itself in {target.parent}. Drag the new version "
                "from the release page into Applications instead."
            )
        old = target.with_name(f".Bridge-{self.current}-old.app")
        self.spawn(
            [
                "/bin/sh",
                "-c",
                SWAP,
                "bridge-update",
                str(os.getpid()),
                str(target),
                str(self._ready),
                str(old),
            ],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._set(state="installing")
        restart()
        return self.overview()

    # Every day --------------------------------------------------------------------------------

    async def tick(self) -> None:
        if not self.supported or not getattr(self.settings, "updates_automatic", True):
            return
        await self.check()
        if self.state["state"] == "available":
            try:
                await self.download()  # Ready before you're asked.
            except UpdateError:
                return
        latest = self.state["latest"]
        if self.state["state"] == "ready" and latest != self._told and self.notify is not None:
            self._told = latest
            await self.notify(
                f"Bridge {latest} is ready",
                (self.state["notes"] or "A new version of Bridge.")[:160]
                + " Restart Bridge to update.",
                "updates",
            )

    async def run(self, first: float = 120) -> None:
        await asyncio.sleep(first)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(CHECK_EVERY)
