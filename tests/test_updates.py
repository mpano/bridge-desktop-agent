"""Updates and releases. Fakes only: nothing is downloaded, signed, notarized or replaced."""

import hashlib
import io
import json
import plistlib
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.desktop import release
from app.desktop.updates import (
    BUNDLE_ID,
    UpdateError,
    Updates,
    newer,
    read_manifest,
    team_of,
    verify,
)

TEAM = "ABCDE12345"
SIGNED = f"Authority=Developer ID Application: Mpano (ABCDE12345)\nTeamIdentifier={TEAM}\n"


def done(code=0, out="", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


def make_app(folder: Path, version="0.2.0", bundle_id=BUNDLE_ID) -> Path:
    app = folder / "Bridge.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version})
    )
    return app


class FakeMac:
    """codesign, spctl and ditto, answering like a Mac would."""

    def __init__(
        self, team=TEAM, new_team=TEAM, signature_ok=True, notarized=True, version="0.2.0"
    ):
        self.team, self.new_team = team, new_team
        self.signature_ok, self.notarized, self.version = signature_ok, notarized, version
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        tool = Path(command[0]).name
        if tool == "codesign" and "-dv" in command:
            team = self.team if "Current.app" in command[-1] else self.new_team
            return done(err=SIGNED.replace(TEAM, team) if team else "Signature=adhoc\n")
        if tool == "codesign":
            return done(0 if self.signature_ok else 1)
        if tool == "spctl":
            return done(0 if self.notarized else 3)
        if tool == "ditto":
            make_app(Path(command[-1]), self.version)
            return done()
        raise AssertionError(command)


def test_versions_and_the_release_information():
    assert newer("0.10.0", "0.9.9") and not newer("0.2.0", "0.2.0") and not newer("0.1", "0.2")
    good = {
        "version": "0.2.0",
        "url": "https://github.com/mpano/x/releases/download/v0.2.0/B.zip",
        "sha256": "a" * 64,
        "notes": "  Faster\n sync ",
    }
    assert read_manifest(good)["notes"] == "Faster sync"
    for bad in (
        {**good, "url": "http://github.com/x.zip"},
        {**good, "url": "https://evil.example/x.zip"},
        {**good, "sha256": "nope"},
        {**good, "version": "soon"},
        [],
    ):
        with pytest.raises(UpdateError):
            read_manifest(bad)


def test_only_a_notarized_bridge_from_the_same_developer_is_accepted(tmp_path):
    app = make_app(tmp_path)
    assert team_of(app, FakeMac(new_team=TEAM)) == TEAM
    assert team_of(app, FakeMac(new_team="")) == ""
    verify(app, "0.2.0", TEAM, FakeMac())
    for mac, version, team, says in [
        (FakeMac(), "0.2.0", "", "isn't signed with a Developer ID"),
        (FakeMac(), "0.3.0", TEAM, "isn't the version announced"),
        (FakeMac(signature_ok=False), "0.2.0", TEAM, "signature is broken"),
        (FakeMac(new_team="ZZZZZ99999"), "0.2.0", TEAM, "wasn't signed by Bridge's developer"),
        (FakeMac(notarized=False), "0.2.0", TEAM, "hasn't approved"),
    ]:
        with pytest.raises(UpdateError, match=says):
            verify(app, version, team, mac)
    other = make_app(tmp_path / "other", bundle_id="com.evil.app")
    with pytest.raises(UpdateError, match="isn't Bridge"):
        verify(other, "0.2.0", TEAM, FakeMac())


def release_server(payload: bytes, manifest: dict | None, feed_status=200, redirect=None):
    def handle(request: httpx.Request):
        if request.url.path.endswith("latest.json"):
            if redirect:
                return httpx.Response(302, headers={"location": redirect})
            if manifest is None:
                return httpx.Response(feed_status)
            return httpx.Response(200, json=manifest)
        if request.url.host == "evil.example":
            return httpx.Response(200, json={})
        return httpx.Response(200, content=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True)


def bundle_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Bridge.app/Contents/Info.plist", "x")
    return buffer.getvalue()


def updater(tmp_path, payload=b"", manifest=None, mac=None, **server):
    current = tmp_path / "Applications/Current.app"
    current.mkdir(parents=True)
    spawn = Mock()
    found = Updates(
        SimpleNamespace(updates_automatic=True, update_feed=""),
        current="0.1.0",
        bundle=current,
        http=release_server(payload, manifest, **server),
        run=mac or FakeMac(),
        spawn=spawn,
        cache=tmp_path / "cache",
    )
    return found, spawn


def announce(payload: bytes, version="0.2.0", **extra) -> dict:
    return {
        "version": version,
        "url": f"https://github.com/mpano/bridge-desktop-agent/releases/download/v{version}/B.zip",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "notes": "Health page and faster sync.",
        **extra,
    }


async def test_check_download_and_restart(tmp_path):
    payload = bundle_zip()
    updates, spawn = updater(tmp_path, payload, announce(payload))
    assert (await updates.check())["state"] == "available"
    with pytest.raises(UpdateError, match="Download the update first"):
        updates.install()
    ready = await updates.download()
    assert ready["state"] == "ready" and ready["latest"] == "0.2.0"
    restart = Mock()
    updates.restart = restart
    assert updates.install()["state"] == "installing"
    restart.assert_called_once()
    command = spawn.call_args.args[0]
    assert command[:2] == ["/bin/sh", "-c"] and command[5].endswith("Current.app")
    assert command[6].endswith("Bridge.app") and "Trash" in command[2]
    assert spawn.call_args.kwargs["start_new_session"] is True


async def test_nothing_new_nothing_released_or_tampered(tmp_path):
    payload = bundle_zip()
    same, _ = updater(tmp_path / "a", payload, announce(payload, version="0.1.0"))
    assert (await same.check())["state"] == "current"
    none, _ = updater(tmp_path / "b", payload, None, feed_status=404)
    assert (await none.check())["state"] == "idle"
    away, _ = updater(tmp_path / "c", payload, None, redirect="https://evil.example/latest.json")
    assert (await away.check())["state"] == "error"
    tampered, _ = updater(tmp_path / "d", payload + b"!", announce(payload))
    with pytest.raises(UpdateError, match="doesn't match the release"):
        await tampered.download()
    assert not list((tmp_path / "d/cache").iterdir())  # Thrown away.
    stranger, _ = updater(
        tmp_path / "e", payload, announce(payload), mac=FakeMac(new_team="ZZZZZ99999")
    )
    with pytest.raises(UpdateError, match="Bridge's developer"):
        await stranger.download()
    unsigned, _ = updater(tmp_path / "f", payload, announce(payload), mac=FakeMac(team=""))
    with pytest.raises(UpdateError, match="Developer ID"):
        await unsigned.download()


async def test_the_daily_check_says_once_when_a_version_is_ready(tmp_path):
    payload = bundle_zip()
    updates, _ = updater(tmp_path, payload, announce(payload))
    updates.notify = AsyncMock()
    await updates.tick()
    await updates.tick()
    updates.notify.assert_awaited_once()
    title, message, view = updates.notify.await_args.args
    assert title == "Bridge 0.2.0 is ready" and "Restart Bridge" in message and view == "updates"
    updates.settings.updates_automatic = False
    off, _ = updater(tmp_path / "off", payload, announce(payload))
    off.settings.updates_automatic = False
    await off.tick()
    assert off.state["checked_at"] == 0


async def test_a_copy_run_from_code_updates_with_git(tmp_path):
    updates = Updates(SimpleNamespace(), bundle=None)
    assert updates.overview()["supported"] is False
    with pytest.raises(UpdateError, match="git"):
        await updates.download()
    await updates.tick()  # Does nothing, and never goes online.
    assert updates.state["checked_at"] == 0


def test_update_routes(tmp_path):
    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.bootstrap import build_agent
    from app.config.settings import Settings

    settings = Settings(_env_file=None, database_path=tmp_path / "db", api_token="token")
    agent = build_agent(settings, llm=AsyncMock(), runner=AsyncMock())
    agent.scheduler = agent.proactive = None
    headers = {"Authorization": "Bearer token"}
    with TestClient(create_app(settings, agent, enable_ui=True)) as client:
        data = client.get("/api/v1/updates", headers=headers).json()
        assert data["supported"] is False and data["current"] and data["automatic"] is True
        assert client.post("/api/v1/updates/install", headers=headers).status_code == 409
        assert client.post("/api/v1/updates/download", headers=headers).status_code == 409
        assert client.get("/api/v1/updates").status_code == 401


# Releases ---------------------------------------------------------------------------------------


def test_finding_the_developer_id():
    listed = (
        '  1) AAAA "Apple Development: Mpano (XYZ)"\n'
        '  2) BBBB "Developer ID Application: Mpano Akim (ABCDE12345)"\n'
        "     2 valid identities found\n"
    )
    assert release.developer_ids(listed) == [
        ("Developer ID Application: Mpano Akim (ABCDE12345)", TEAM)
    ]
    with pytest.raises(release.ReleaseError, match="Apple Developer Program"):
        release.find_identity(lambda *a, **k: done(out="0 valid identities found"))


def test_signing_goes_inside_out_with_the_hardened_runtime(tmp_path):
    app = tmp_path / "Bridge.app"
    deep = app / "Contents/Frameworks/lib/deep.dylib"
    deep.parent.mkdir(parents=True)
    deep.write_bytes(b"\xcf\xfa\xed\xfe" + b"0" * 10)
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/Bridge").write_bytes(b"\xcf\xfa\xed\xfe" + b"0" * 10)
    (app / "Contents/Resources").mkdir()
    (app / "Contents/Resources/readme.txt").write_text("not a binary")
    assert release.binaries(app) == [deep, app / "Contents/MacOS/Bridge"]
    run = Mock(return_value=done())
    release.sign(app, "Developer ID Application: Mpano (ABCDE12345)", tmp_path / "e.plist", run)
    commands = [c.args[0] for c in run.call_args_list]
    assert [c[-1] for c in commands] == [
        str(deep),
        str(app / "Contents/MacOS/Bridge"),
        str(app),
        str(app),
    ]
    assert all("runtime" in c and "--timestamp" in c for c in commands[:3])
    assert "--entitlements" in commands[2] and "--verify" in commands[3]
    assert release.ENTITLEMENTS["com.apple.security.device.audio-input"] is True


def test_notarizing_reports_apples_log(tmp_path):
    accepted = Mock(return_value=done(out=json.dumps({"id": "1", "status": "Accepted"})))
    release.notarize(tmp_path / "B.zip", "bridge-notary", accepted)
    assert "--keychain-profile" in accepted.call_args.args[0]
    answers = iter(
        [done(out=json.dumps({"id": "9", "status": "Invalid"})), done(out="binary not signed")]
    )
    with pytest.raises(release.ReleaseError, match="binary not signed"):
        release.notarize(tmp_path / "B.zip", "bridge-notary", lambda *a, **k: next(answers))


def test_the_release_information_matches_the_download(tmp_path):
    archive = tmp_path / "Bridge-0.2.0.zip"
    archive.write_bytes(b"the app")
    info = release.manifest("0.2.0", archive, "Health page.\n")
    assert read_manifest(info)["url"].endswith("/releases/download/v0.2.0/Bridge-0.2.0.zip")
    assert info["sha256"] == hashlib.sha256(b"the app").hexdigest() and info["size"] == 7
