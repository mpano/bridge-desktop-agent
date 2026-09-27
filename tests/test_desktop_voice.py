from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.desktop.service import ServiceState, ServiceStatus
from app.desktop.voice import MenuVoiceService, VoiceState


class Secret:
    def __init__(self, value: str):
        self.value = value

    def get_secret_value(self) -> str:
        return self.value


class FakeThread:
    def __init__(self, *, target, name, daemon):
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started

    def join(self, timeout=None):
        self.started = False


def settings(model_path: Path | None = None, token: str = "secret"):
    return SimpleNamespace(
        api_token=Secret(token),
        voice_wake_word_model_path=model_path,
    )


def test_menu_voice_requires_installed_bridge_model(tmp_path):
    local = Mock()
    local.status = ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")
    voice = MenuVoiceService(settings(tmp_path / "missing.onnx"), local)

    assert voice.start() is False
    assert voice.status.state == VoiceState.FAILED
    assert "Bridge" in voice.status.message
    local.start.assert_not_called()


def test_menu_voice_start_is_explicit_and_starts_local_service(tmp_path, monkeypatch):
    model = tmp_path / "bridge.onnx"
    model.write_bytes(b"model")
    local = Mock()
    local.status = ServiceStatus(ServiceState.STOPPED, "Stopped")
    monkeypatch.setattr("app.desktop.voice.threading.Thread", FakeThread)
    voice = MenuVoiceService(settings(model), local)

    assert voice.status.state == VoiceState.STOPPED
    assert voice.start() is True
    local.start.assert_called_once()
    assert voice.status.state == VoiceState.STARTING
    assert voice._thread.started is True


def test_menu_voice_requires_api_token(tmp_path):
    model = tmp_path / "bridge.onnx"
    model.write_bytes(b"model")
    local = Mock()
    local.status = ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")
    voice = MenuVoiceService(settings(model, token=""), local)

    assert voice.start() is False
    assert voice.status.state == VoiceState.FAILED
    assert "API_TOKEN" in voice.status.message
