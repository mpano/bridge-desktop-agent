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


def test_typed_request_runs_through_local_api_and_captures_approval(monkeypatch):
    local = Mock()
    local.status = ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")
    sent = []

    class FakeAgent:
        def __init__(self, service, token, stop):
            assert token == "secret"

        async def message(self, text):
            sent.append(text)
            return {
                "status": "confirmation_required",
                "message": "Review this",
                "confirmation": {"token": "t", "action": "email_send", "arguments": {}},
            }

    monkeypatch.setattr("app.desktop.voice.LocalAPIAgent", FakeAgent)
    voice = MenuVoiceService(settings(), local)
    assert voice.submit_text("  send   the report ") is True
    assert voice.wait(2)
    assert sent == ["send the report"]
    assert voice.status.transcript == "send the report"
    assert voice.status.state == VoiceState.APPROVAL
    assert voice.pending_review.action == "email_send"
    # Nothing else starts until the pending action is reviewed.
    assert voice.submit_text("another") is False


def test_typed_request_ignores_blank_text_and_missing_token():
    local = Mock()
    local.status = ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")
    assert MenuVoiceService(settings(), local).submit_text("   ") is False
    voice = MenuVoiceService(settings(token=""), local)
    assert voice.submit_text("hello") is False
    assert "API_TOKEN" in voice.status.message
