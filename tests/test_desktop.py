import asyncio
import errno
import plistlib
import shlex
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.agent import Agent
from app.api.launch import LaunchTickets
from app.config.settings import Settings
from app.desktop.bundle import build_launcher
from app.desktop.login import LoginItem, running_app_bundle
from app.desktop.menubar import MenuBarController
from app.desktop.service import LocalService, ServiceState, ServiceStatus
from app.desktop.voice import VoiceState


class FakeItem:
    def __init__(self, title, callback=None):
        self.title, self.callback = title, callback

    def set_callback(self, callback):
        self.callback = callback


class FakeApp:
    def __init__(self, *args, **kwargs):
        self.title = kwargs.get("title")
        self.menu = []

    def run(self):
        pass


class FakeTimer:
    def __init__(self, callback, interval):
        self.callback = callback
        self.start = Mock()
        self.stop = Mock()


def make_menu():
    native = SimpleNamespace(
        App=FakeApp, MenuItem=FakeItem, Timer=FakeTimer, alert=Mock(), quit_application=Mock()
    )
    service = Mock()
    service.status = ServiceStatus(ServiceState.STOPPED, "Stopped")
    service.wait.return_value = True
    service.dashboard_url.return_value = None
    browser = Mock(return_value=True)
    menu = MenuBarController(service, native, browser)
    return menu, service, native, browser


def test_menu_opens_dashboard_with_single_use_ticket_not_token():
    menu, service, _, browser = make_menu()
    service.status = ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")
    service.dashboard_url.return_value = "http://127.0.0.1:8000/#launch=abc"
    menu.open_dashboard()
    browser.assert_called_once_with("http://127.0.0.1:8000/#launch=abc")


def test_local_service_dashboard_url_requires_running_service():
    service = LocalService(Settings(_env_file=None, api_token="local-secret"))
    assert service.dashboard_url() is None
    service._status = ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")
    url = service.dashboard_url()
    assert url.startswith("http://localhost:8000/#launch=")
    assert "local-secret" not in url
    assert service.tickets.redeem(url.split("=", 1)[1])


def test_launch_tickets_are_single_use_and_expire():
    now = [100.0]
    tickets = LaunchTickets(clock=lambda: now[0])
    first = tickets.issue()
    assert tickets.redeem(first)
    assert not tickets.redeem(first)
    second = tickets.issue()
    now[0] += LaunchTickets.TTL_SECONDS + 1
    assert not tickets.redeem(second)
    assert not tickets.redeem("ünicode-ticket-value")


def test_launch_ticket_starts_a_session_once_and_never_returns_the_token(tmp_path):
    from fastapi.testclient import TestClient

    from app.api.server import create_app

    tickets = LaunchTickets()
    settings = Settings(_env_file=None, api_token="local-secret", database_path=tmp_path / "db")
    app = create_app(settings, AsyncMock(), enable_ui=True, launch_tickets=tickets)
    origin = {"origin": "http://localhost:8000"}
    with TestClient(app, base_url="http://localhost:8000") as client:
        ticket = tickets.issue()
        assert client.post("/api/v1/session/launch", json={"ticket": ticket}).status_code == 403
        evil = {"origin": "http://evil.example"}
        refused = client.post("/api/v1/session/launch", json={"ticket": ticket}, headers=evil)
        assert refused.status_code == 403
        ok = client.post("/api/v1/session/launch", json={"ticket": ticket}, headers=origin)
        assert ok.json() == {"signed_in": False, "setup": True}
        assert "local-secret" not in ok.text
        assert "bridge_session" in ok.cookies
        again = client.post("/api/v1/session/launch", json={"ticket": ticket}, headers=origin)
        assert again.status_code == 401


def test_launch_endpoint_absent_without_owner():
    from fastapi.testclient import TestClient

    from app.api.server import create_app

    settings = Settings(_env_file=None, api_token="local-secret")
    with TestClient(create_app(settings, AsyncMock(), enable_ui=True)) as client:
        response = client.post("/api/v1/session/launch", json={"ticket": "x" * 20})
        assert response.status_code in {404, 405}


def test_menu_only_opens_owned_running_dashboard():
    menu, service, _, browser = make_menu()
    menu.open_dashboard()
    browser.assert_not_called()
    service.status = ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")
    menu.refresh()
    assert menu.start_item.callback is None
    assert menu.stop_item.callback is not None
    menu.open_dashboard()
    browser.assert_called_once_with("http://127.0.0.1:8000")
    assert "token" not in browser.call_args.args[0]


def test_quit_waits_without_blocking_menu():
    menu, service, native, _ = make_menu()
    service.status = ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")
    service.wait.return_value = False
    menu.quit()
    service.stop.assert_called_once()
    native.quit_application.assert_not_called()
    assert menu.open_item.callback is None
    service.status = ServiceStatus(ServiceState.STOPPED, "Stopped")
    menu.refresh()
    native.quit_application.assert_not_called()
    service.wait.return_value = True
    menu.refresh()
    native.quit_application.assert_called_once()
    service.wait.assert_called_with(timeout=0)


def test_menu_failed_start_can_retry_or_show_details():
    menu, service, native, _ = make_menu()
    service.status = ServiceStatus(ServiceState.FAILED, "Port is already in use.")
    menu.refresh()
    assert menu.start_item.callback is not None
    assert menu.open_item.callback is None
    menu.details()
    assert "Port" in native.alert.call_args.kwargs["message"]
    menu.start()
    service.start.assert_called_once()


def test_missing_token_does_not_launch_worker():
    service = LocalService(Settings(_env_file=None, api_token=""))
    assert not service.start()
    assert service.status.state == ServiceState.FAILED
    assert "API_TOKEN" in service.status.message
    assert service.wait(timeout=0)


def test_start_idempotency_and_nonblocking_stop(monkeypatch):
    service = LocalService(Settings(_env_file=None, api_token="local-secret"))
    started = threading.Event()

    def worker():
        started.set()
        service._stop.wait(2)
        service._update(ServiceState.STOPPED, "Stopped")

    monkeypatch.setattr(service, "_run", worker)
    try:
        assert service.start()
        assert started.wait(1)
        assert not service.start()
        service.stop()
        assert service.wait(2)
        assert service.status.state == ServiceState.STOPPED
        assert service.start()
    finally:
        service.stop()
        assert service.wait(2)


@pytest.mark.parametrize(
    "error,message",
    [
        (OSError(errno.EADDRINUSE, "sensitive-details"), "Port is already in use"),
        (OSError(errno.EACCES, "sensitive-details"), "Cannot bind"),
        (RuntimeError("sensitive-details"), "Service could not start"),
        (SystemExit(3), "Service could not start"),
    ],
)
def test_failures_are_safe_and_do_not_claim_readiness(monkeypatch, error, message):
    service = LocalService(Settings(_env_file=None, api_token="local-secret"))
    monkeypatch.setattr(service, "_serve", AsyncMock(side_effect=error))
    assert service.start()
    assert service.wait(2)
    assert service.status.state == ServiceState.FAILED
    assert message in service.status.message
    assert "sensitive-details" not in service.status.message
    assert service.status.url is None


def test_bundle_quotes_paths_and_does_not_copy_secrets(tmp_path):
    project = tmp_path / "project ' with $spaces"
    (project / "app").mkdir(parents=True)
    (project / "app/main.py").touch()
    (project / ".env").write_text("OPENAI_API_KEY=do-not-copy")
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    target = build_launcher(project, python, tmp_path / "Bridge.app", native=False)
    info = plistlib.loads((target / "Contents/Info.plist").read_bytes())
    assert info["LSUIElement"] is True
    assert info["CFBundleName"] == "Bridge"
    assert info["CFBundleExecutable"] == "Bridge"
    assert "microphone" in info["NSMicrophoneUsageDescription"].lower()
    executable = target / "Contents/MacOS/Bridge"
    text = executable.read_text()
    assert shlex.quote(str(project)) in text
    assert shlex.quote(str(python)) in text
    assert "--menubar" in text
    assert "do-not-copy" not in text
    assert executable.stat().st_mode & 0o111
    assert not (target / ".env").exists()
    with pytest.raises(FileExistsError):
        build_launcher(project, python, target)


def test_bundle_force_replaces_only_bridge_builds(tmp_path):
    project = tmp_path / "project"
    (project / "app").mkdir(parents=True)
    (project / "app/main.py").touch()
    target = build_launcher(project, Path(sys.executable), tmp_path / "Bridge.app", native=False)
    text = (target / "Contents/MacOS/Bridge").read_text()
    assert "BRIDGE_APP_BUNDLE" in text
    assert "Library/Logs/Bridge" in text
    rebuilt = build_launcher(project, Path(sys.executable), target, replace=True, native=False)
    assert (rebuilt / "Contents/Info.plist").is_file()

    stranger = tmp_path / "Other.app"
    (stranger / "Contents").mkdir(parents=True)
    (stranger / "Contents/keep.txt").write_text("user data")
    with pytest.raises(ValueError):
        build_launcher(project, Path(sys.executable), stranger, replace=True)
    assert (stranger / "Contents/keep.txt").read_text() == "user data"


def test_login_item_writes_and_removes_launch_agent(tmp_path):
    bundle = tmp_path / "Bridge.app"
    bundle.mkdir()
    login = LoginItem(bundle, tmp_path / "LaunchAgents")
    assert login.available and not login.enabled
    login.set_enabled(True)
    assert login.enabled
    definition = plistlib.loads(login.plist.read_bytes())
    assert definition["ProgramArguments"] == [
        "/usr/bin/open",
        "-a",
        str(bundle),
        "--args",
        "--login",
    ]
    assert definition["RunAtLoad"] is True
    login.set_enabled(False)
    assert not login.plist.exists()

    unavailable = LoginItem(None, tmp_path / "LaunchAgents")
    assert not unavailable.available
    with pytest.raises(ValueError):
        unavailable.set_enabled(True)


def test_running_app_bundle_reads_launcher_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("BRIDGE_APP_BUNDLE", raising=False)
    assert running_app_bundle() is None
    bundle = tmp_path / "Bridge.app"
    bundle.mkdir()
    monkeypatch.setenv("BRIDGE_APP_BUNDLE", str(bundle))
    assert running_app_bundle() == bundle
    monkeypatch.setenv("BRIDGE_APP_BUNDLE", str(tmp_path))
    assert running_app_bundle() is None


def test_menu_open_at_login_toggle(tmp_path):
    native = SimpleNamespace(
        App=FakeApp, MenuItem=FakeItem, Timer=FakeTimer, alert=Mock(), quit_application=Mock()
    )
    service = Mock()
    service.status = ServiceStatus(ServiceState.STOPPED, "Stopped")
    bundle = tmp_path / "Bridge.app"
    bundle.mkdir()
    login = LoginItem(bundle, tmp_path / "LaunchAgents")
    menu = MenuBarController(service, native, Mock(), login=login)
    assert menu.login_item in menu.app.menu
    assert menu.login_item.state == 0
    menu.toggle_login()
    assert login.enabled and menu.login_item.state == 1
    menu.toggle_login()
    assert not login.enabled and menu.login_item.state == 0

    detached = MenuBarController(service, native, Mock(), login=LoginItem(None, tmp_path))
    assert detached.login_item.callback is None


def test_bundle_invalid_project_is_not_created(tmp_path):
    destination = tmp_path / "Agent.app"
    with pytest.raises(ValueError):
        build_launcher(tmp_path, __import__("pathlib").Path(sys.executable), destination)
    assert not destination.exists()


async def test_agent_close_waits_for_active_execution():
    llm = AsyncMock()
    workflows = Mock()
    agent = Agent(SimpleNamespace(llm=llm), Mock(), workflows=workflows)
    async with agent.lock:
        closing = asyncio.create_task(agent.close())
        await asyncio.sleep(0)
        workflows.close.assert_not_called()
        llm.close.assert_not_called()
    await closing
    workflows.close.assert_called_once()
    llm.close.assert_awaited_once()


def test_menu_voice_start_stop_is_explicit():
    menu, service, native, browser = make_menu()
    voice = Mock()
    voice.status = SimpleNamespace(state=VoiceState.STOPPED, message="off")
    voice.wait.return_value = True
    menu = MenuBarController(service, native, browser, voice=voice)
    assert menu.voice_start_item.callback is not None
    assert menu.voice_stop_item.callback is None

    menu.start_voice()
    voice.start.assert_called_once()
    voice.status = SimpleNamespace(state=VoiceState.LISTENING, message="Listening for “Bridge”")
    menu.refresh()
    assert "Bridge" in menu.voice_status_item.title
    assert menu.voice_start_item.callback is None
    assert menu.voice_stop_item.callback is not None

    menu.stop_voice()
    voice.stop.assert_called_once()


def test_browser_failure_is_explained_without_crashing():
    menu, service, native, browser = make_menu()
    service.status = ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")
    browser.side_effect = OSError("No browser")
    menu.open_dashboard()
    assert "http://127.0.0.1:8000" in native.alert.call_args.kwargs["message"]


def test_standalone_first_launch_creates_private_settings_with_token(tmp_path, monkeypatch):
    from app.desktop import standalone

    template = tmp_path / "bundle" / ".env.example"
    template.parent.mkdir()
    template.write_text("OPENAI_API_KEY=\nAPI_TOKEN=\nDATABASE_PATH=./somewhere.db\n")
    monkeypatch.setattr(standalone, "resource", lambda name: template.parent / name)
    env, created = standalone.ensure_settings(tmp_path / "support")
    text = env.read_text()
    assert created and (env.stat().st_mode & 0o777) == 0o600
    token = next(line for line in text.splitlines() if line.startswith("API_TOKEN="))
    assert len(token) > len("API_TOKEN=") + 30
    assert "DATABASE_PATH=./agent.db" in text
    again, created_again = standalone.ensure_settings(tmp_path / "support")
    assert again == env and not created_again and again.read_text() == text


def test_package_bundles_resources_and_skips_unused_heavy_modules(tmp_path):
    from app.desktop.package import pyinstaller_args

    args = pyinstaller_args(tmp_path / "Bridge.icns", tmp_path / "dist", tmp_path / "work")
    joined = " ".join(args)
    assert "--exclude-module=playwright" in args
    assert "app/ui/static" in joined and ".env.example" in joined
    assert "--osx-bundle-identifier=app.bridge.desktop-agent" in args
    assert ".env:" not in joined  # Never ship the real settings file.


@pytest.mark.skipif(
    sys.platform != "darwin" or not __import__("shutil").which("clang"),
    reason="Needs macOS and the Xcode command-line tools",
)
def test_native_launcher_gives_bridge_its_own_executable(tmp_path):
    project = tmp_path / "project with spaces"
    (project / "app").mkdir(parents=True)
    (project / "app/main.py").touch()
    (project / ".env").write_text("OPENAI_API_KEY=do-not-copy")
    target = build_launcher(project, Path(sys.executable), tmp_path / "Bridge.app", native=True)
    executable = target / "Contents/MacOS/Bridge"
    binary = executable.read_bytes()
    assert binary[:4] in {b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}  # Mach-O, not a script.
    assert str(project).encode() in binary
    assert b"do-not-copy" not in binary
    assert executable.stat().st_mode & 0o111


def test_hey_bridge_switch_is_remembered(tmp_path):
    from app.desktop.menubar import remember_listening, was_listening

    path = tmp_path / "listening.json"
    assert was_listening(path) is False  # Off until you turn it on.
    remember_listening(True, path)
    assert was_listening(path) is True
    remember_listening(False, path)
    assert was_listening(path) is False
    path.write_text("not json")
    assert was_listening(path) is False


def test_bundle_signs_with_your_certificate_when_it_exists(monkeypatch, tmp_path):
    from app.desktop import bundle

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        stdout = '  1) ABC "Bridge Local Signing"\n' if command[1] == "find-identity" else ""
        return SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(bundle.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(bundle.subprocess, "run", run)
    assert bundle._sign(tmp_path) == "Bridge Local Signing"
    assert calls[-1][:4] == ["/usr/bin/codesign", "--force", "--sign", "Bridge Local Signing"]
    assert "app.bridge.desktop-agent" in calls[-1]

    calls.clear()
    monkeypatch.setattr(bundle, "signing_identity", lambda: None)
    assert bundle._sign(tmp_path) == "ad-hoc"  # No certificate: same as before.
    assert calls[-1][3] == "-"
