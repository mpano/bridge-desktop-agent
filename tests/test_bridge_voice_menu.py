from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.desktop.service import ServiceState, ServiceStatus
from app.desktop.voice import MenuVoiceService, VoiceState
from app.voice import paths


def voice_settings(**overrides):
    values = {
        "api_token": SimpleNamespace(get_secret_value=lambda: "local-secret"),
        "voice_wake_word_model_path": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_install_wakeword_copies_to_private_bridge_support_path(tmp_path, monkeypatch):
    source = tmp_path / "trained-bridge.onnx"
    source.write_bytes(b"reviewed-model")
    destination = tmp_path / "Bridge" / "wakewords" / "bridge.onnx"
    monkeypatch.setattr(paths, "DEFAULT_WAKE_WORD_MODEL", destination)

    installed = paths.install_wake_word_model(source)

    assert installed == destination.resolve()
    assert installed.read_bytes() == b"reviewed-model"
    assert installed.stat().st_mode & 0o077 == 0


def test_install_wakeword_rejects_non_onnx(tmp_path):
    source = tmp_path / "bridge.bin"
    source.write_bytes(b"not-onnx")
    try:
        paths.install_wake_word_model(source)
    except ValueError as exc:
        assert ".onnx" in str(exc)
    else:
        raise AssertionError("non-ONNX wake-word model was accepted")


def test_menu_voice_does_not_start_without_installed_model(tmp_path):
    local = Mock()
    local.status = ServiceStatus(ServiceState.RUNNING, "running", "http://127.0.0.1:8000")
    settings = voice_settings(voice_wake_word_model_path=tmp_path / "missing.onnx")
    service = MenuVoiceService(settings, local)

    assert service.start() is False
    assert service.status.state == VoiceState.FAILED
    assert "wake-word model" in service.status.message
    local.start.assert_not_called()


def test_menu_voice_start_is_explicit_and_nonblocking(tmp_path, monkeypatch):
    model = tmp_path / "bridge.onnx"
    model.write_bytes(b"model")
    local = Mock()
    local.status = ServiceStatus(ServiceState.RUNNING, "running", "http://127.0.0.1:8000")
    settings = voice_settings(voice_wake_word_model_path=model)
    service = MenuVoiceService(settings, local)

    def fake_run():
        service._update(VoiceState.LISTENING, 'Listening for “Bridge”')
        service._stop.wait(1)
        service._update(VoiceState.STOPPED, "Voice listening is off")

    monkeypatch.setattr(service, "_run", fake_run)

    assert service.status.state == VoiceState.STOPPED
    assert service.start() is True
    assert service.wait(timeout=0) is False
    service.stop()
    assert service.wait(timeout=2) is True
    assert service.status.state == VoiceState.STOPPED
