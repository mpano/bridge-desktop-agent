"""Optional training checks; no speech services, microphone, or model downloads."""

import importlib.util
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def trainer():
    for dependency in ("numpy", "onnx", "onnxruntime", "openwakeword", "sklearn", "scipy"):
        pytest.importorskip(dependency)
    path = Path(__file__).resolve().parents[1] / "scripts" / "train_wakeword_local.py"
    spec = importlib.util.spec_from_file_location("bridge_training", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("sample_count", [0, 3200])
def test_empty_or_silent_exports_cannot_become_training_data(
    trainer, tmp_path, monkeypatch, sample_count
):
    def export(command, **kwargs):
        output = Path(command[command.index("-o") + 1])
        trainer.wavfile.write(output, 16000, trainer.np.zeros(sample_count, dtype="int16"))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(trainer.subprocess, "run", export)
    with pytest.raises(ValueError, match="empty or silent"):
        trainer.synthesize(tmp_path, "Synthetic test voice", "Hey Bridge", 180)


def test_onnx_export_matches_trained_probabilities(trainer, tmp_path):
    import onnxruntime

    rng = trainer.np.random.default_rng(12)
    samples = rng.normal(size=(30, 16 * 96)).astype("float32")
    labels = trainer.np.arange(30) % 2
    scaler = trainer.StandardScaler().fit(samples)
    classifier = trainer.MLPClassifier(
        hidden_layer_sizes=(64, 32), max_iter=50, random_state=0
    ).fit(scaler.transform(samples), labels)
    path = tmp_path / "hey_bridge.onnx"
    trainer.export_model(path, scaler, classifier)
    session = onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    for sample in samples[:3]:
        actual = session.run(None, {"features": sample.reshape(1, 16, 96)})[0][0, 0]
        expected = classifier.predict_proba(scaler.transform(sample.reshape(1, -1)))[0, 1]
        assert actual == pytest.approx(expected, abs=1e-5)


def test_timed_out_export_is_not_cached(trainer, tmp_path, monkeypatch):
    def export(command, **kwargs):
        Path(command[command.index("-o") + 1]).write_bytes(b"partial")
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(trainer.subprocess, "run", export)
    with pytest.raises(subprocess.TimeoutExpired):
        trainer.synthesize(tmp_path, "Synthetic test voice", "Hey Bridge", 180)
    assert not list(tmp_path.iterdir())
