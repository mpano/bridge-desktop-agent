import pytest

from app.voice import paths


def test_install_bridge_wake_word_model(tmp_path, monkeypatch):
    source = tmp_path / "trained.onnx"
    source.write_bytes(b"onnx-model")
    destination = tmp_path / "support" / "wakewords" / "bridge.onnx"
    monkeypatch.setattr(paths, "DEFAULT_WAKE_WORD_MODEL", destination)

    installed = paths.install_wake_word_model(source)

    assert installed == destination.resolve()
    assert installed.read_bytes() == b"onnx-model"
    assert installed.stat().st_mode & 0o077 == 0
    assert paths.wake_word_model_path(None) == destination.resolve()


@pytest.mark.parametrize("name", ["bridge.txt", "missing.onnx"])
def test_install_bridge_wake_word_rejects_invalid_input(tmp_path, monkeypatch, name):
    destination = tmp_path / "support" / "wakewords" / "bridge.onnx"
    monkeypatch.setattr(paths, "DEFAULT_WAKE_WORD_MODEL", destination)
    source = tmp_path / name
    if source.suffix != ".onnx":
        source.write_bytes(b"bad")

    with pytest.raises(ValueError, match=".onnx"):
        paths.install_wake_word_model(source)
