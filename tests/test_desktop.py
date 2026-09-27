import asyncio
import errno
import plistlib
import shlex
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.agent import Agent
from app.config.settings import Settings
from app.desktop.bundle import build_launcher
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
    browser = Mock(return_value=True)
    menu = MenuBarController(service, native, browser)
    return menu, service, native, browser


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
    target = build_launcher(project, python, tmp_path / "Bridge.app")
    info = plistlib.loads((target / "Contents/Info.plist").read_bytes())
    assert info["LSUIElement"] is True
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
    voice.status = SimpleNamespace(state=VoiceState.LISTENING, message='Listening for “Bridge”')
    menu.refresh()
    assert 'Bridge' in menu.voice_status_item.title
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
