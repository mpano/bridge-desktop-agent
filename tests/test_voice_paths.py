import stat
from pathlib import Path

import pytest

from app.voice import paths


def test_default_bridge_wakeword_path_is_under_bridge_support():
    expected = Path.home() / "Library/Application Support/Bridge/wakewords/hey_bridge.onnx"
    assert paths.wake_word_model_path(None) == expected.resolve()


def test_configured_wakeword_path_wins(tmp_path):
    model = tmp_path / "custom.onnx"
    assert paths.wake_word_model_path(model) == model.resolve()


def test_install_bridge_wakeword_copies_privately(tmp_path, monkeypatch):
    source = tmp_path / "trained-bridge.onnx"
    source.write_bytes(b"reviewed-model")
    destination = tmp_path / "Bridge" / "wakewords" / "bridge.onnx"
    monkeypatch.setattr(paths, "DEFAULT_WAKE_WORD_MODEL", destination)

    installed = paths.install_wake_word_model(source)

    assert installed == destination
    assert installed.read_bytes() == b"reviewed-model"
    assert stat.S_IMODE(installed.stat().st_mode) == 0o600
    assert stat.S_IMODE(installed.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("name", ["bridge.txt", "bridge.tflite"])
def test_install_bridge_wakeword_requires_onnx(tmp_path, name):
    source = tmp_path / name
    source.write_bytes(b"not-onnx")
    with pytest.raises(ValueError, match=".onnx"):
        paths.install_wake_word_model(source)
