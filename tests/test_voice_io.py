import asyncio
import sys
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.voice.audio import MicrophoneRecorder
from app.voice.tts import MacOSSayTTS
from app.voice.wakeword import OpenWakeWordDetector


@pytest.mark.parametrize("seconds", [0, -1, 31, float("inf"), float("nan")])
async def test_recording_duration_is_bounded(seconds):
    with pytest.raises(ValueError):
        await MicrophoneRecorder().record_wav(seconds)


async def test_record_cancel_waits_for_worker_and_removes_wav(tmp_path, monkeypatch):
    started, release = Event(), Event()
    path = tmp_path / "recording.wav"
    recorder = MicrophoneRecorder()

    def record(seconds):
        started.set()
        assert release.wait(2)
        path.write_bytes(b"audio")
        return path

    monkeypatch.setattr(recorder, "_record_wav", record)
    task = asyncio.create_task(recorder.record_wav(1))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not path.exists()


@pytest.mark.parametrize("name", ["missing.onnx", "model.tflite", "directory.onnx"])
def test_wakeword_requires_onnx_file(tmp_path, name):
    path = tmp_path / name
    if name == "model.tflite":
        path.touch()
    elif name == "directory.onnx":
        path.mkdir()
    with pytest.raises(RuntimeError, match=".onnx"):
        OpenWakeWordDetector(path)


async def test_wake_cancel_closes_microphone_and_selects_onnx(tmp_path, monkeypatch):
    path = tmp_path / "wake.onnx"
    path.touch()
    opened, closed = Event(), Event()
    model_args = []

    class Stream:
        read_available = 0

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            opened.set()
            return self

        def __exit__(self, *args):
            closed.set()

    def model(**kwargs):
        model_args.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=Stream))
    shared = tmp_path / "resources" / "models"
    shared.mkdir(parents=True)
    (shared / "melspectrogram.onnx").touch()
    (shared / "embedding_model.onnx").touch()
    monkeypatch.setitem(
        sys.modules, "openwakeword", SimpleNamespace(__file__=str(tmp_path / "__init__.py"))
    )
    monkeypatch.setitem(sys.modules, "openwakeword.model", SimpleNamespace(Model=model))
    stop = Event()
    task = asyncio.create_task(OpenWakeWordDetector(path).wait(stop))
    async with asyncio.timeout(2):
        while not opened.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stop.is_set() and closed.is_set()
    assert model_args == [{"wakeword_models": [str(path)], "inference_framework": "onnx"}]


def test_wakeword_missing_shared_assets_gives_setup_instructions(tmp_path, monkeypatch):
    path = tmp_path / "wake.onnx"
    path.touch()
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "openwakeword", SimpleNamespace(__file__=str(tmp_path / "__init__.py"))
    )
    model = AsyncMock()
    monkeypatch.setitem(sys.modules, "openwakeword.model", SimpleNamespace(Model=model))
    with pytest.raises(RuntimeError, match="docs/VOICE.md"):
        OpenWakeWordDetector(path)._wait_blocking(Event())
    model.assert_not_called()


async def test_tts_passes_option_like_text_only_via_stdin():
    runner = SimpleNamespace(run=AsyncMock())
    await MacOSSayTTS("Alex", 180, runner=runner).speak("-o /tmp/unwanted.aiff")
    runner.run.assert_awaited_once_with(
        "/usr/bin/say", "-v", "Alex", "-r", "180", input_text="-o /tmp/unwanted.aiff"
    )


async def test_tts_sanitizes_native_failure():
    runner = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("private transcript")))
    with pytest.raises(RuntimeError, match="Text-to-speech failed") as error:
        await MacOSSayTTS(runner=runner).speak("hello")
    assert "private transcript" not in str(error.value)


async def test_tts_skips_empty_and_bounds_input():
    runner = SimpleNamespace(run=AsyncMock())
    tts = MacOSSayTTS(runner=runner)
    await tts.speak("  ")
    runner.run.assert_not_called()
    await tts.speak("a" * 5000)
    assert runner.run.call_args.kwargs["input_text"] == "a" * 4000


def test_recording_failure_stops_microphone(monkeypatch):
    from unittest.mock import Mock

    sounddevice = SimpleNamespace(
        rec=Mock(return_value=[]),
        wait=Mock(side_effect=RuntimeError("device disconnected")),
        stop=Mock(),
    )
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "sounddevice", sounddevice)
    with pytest.raises(RuntimeError, match="Microphone capture failed"):
        MicrophoneRecorder()._record_wav(1)
    sounddevice.stop.assert_called_once_with()
