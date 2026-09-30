import asyncio
import ctypes as ct
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.config.settings import Settings
from app.desktop.hotkey import GlobalVoiceShortcut, HotKeyID
from app.desktop.service import ServiceState, ServiceStatus
from app.desktop.voice import LocalAPIAgent, MenuVoiceService, VoiceState
from app.voice.cue import StartCue
from app.voice.endpointing import EndpointingRecorder, NoSpeechDetected, SilenceEndpoint


def test_silence_endpoint_ignores_click_and_waits_for_sustained_activity():
    endpoint = SilenceEndpoint(wait_seconds=1)
    assert not endpoint.feed(0.9, 0.02)
    assert not endpoint.feed(0, 0.5)
    assert endpoint.feed(0, 0.5)
    assert not endpoint.heard_speech


def test_endpoint_stops_only_after_speech_and_trailing_quiet():
    endpoint = SilenceEndpoint(silence_seconds=0.5)
    assert not endpoint.feed(0.05, 0.25)
    assert endpoint.heard_speech
    assert not endpoint.feed(0, 0.3)
    assert not endpoint.feed(0.05, 0.1)
    assert not endpoint.feed(0, 0.3)
    assert endpoint.feed(0, 0.2)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_invalid_audio_does_not_drive_endpoint(value):
    with pytest.raises(ValueError):
        SilenceEndpoint().feed(value, 0.02)


def test_streaming_capture_closes_before_silence_returns(tmp_path, monkeypatch):
    import sys

    np = pytest.importorskip("numpy")
    events = []
    chunks = [np.full((320, 1), 5000, np.int16)] * 12 + [np.zeros((320, 1), np.int16)] * 60

    class Stream:
        read_available = 320

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            events.append("opened")
            return self

        def __exit__(self, *args):
            events.append("closed")

        def read(self, size):
            return chunks.pop(0), False

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=Stream))
    recorder = EndpointingRecorder(threading.Event(), silence_seconds=0.3)

    def write(samples):
        assert events == ["opened", "closed"]
        assert len(samples) < 16000
        return tmp_path / "recorded.wav"

    monkeypatch.setattr(recorder, "_write_wav", write)
    assert recorder._record_wav(15) == tmp_path / "recorded.wav"
    assert chunks  # Did not wait out the full recording maximum.


async def test_cancel_streaming_recorder_signals_worker(monkeypatch):
    stop = threading.Event()
    started = threading.Event()
    recorder = EndpointingRecorder(stop)

    def record(seconds):
        started.set()
        assert stop.wait(2)
        raise NoSpeechDetected("stopped")

    monkeypatch.setattr(recorder, "_record_wav", record)
    task = asyncio.create_task(recorder.record_wav(15))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stop.is_set()


async def test_start_sound_plays_in_process_without_launching_a_player():
    runner = SimpleNamespace(run=AsyncMock())
    native = Mock(return_value=0.2)
    await StartCue(runner, native=native).play()
    native.assert_called_once()
    runner.run.assert_not_awaited()


async def test_start_sound_falls_back_to_fixed_command():
    runner = SimpleNamespace(run=AsyncMock())
    await StartCue(runner, native=lambda: None).play()
    runner.run.assert_awaited_once_with("/usr/bin/afplay", "/System/Library/Sounds/Tink.aiff")


async def test_start_sound_failure_never_blocks_recording():
    runner = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("internal details")))
    await StartCue(runner, native=None).play()  # Logs and continues; no VoiceError.


def service():
    local = SimpleNamespace(
        status=ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")
    )
    return MenuVoiceService(Settings(_env_file=None, api_token="test-token"), local)


def confirmation():
    return {
        "status": "confirmation_required",
        "message": "Create folder?",
        "confirmation": {
            "token": "private-approval-token",
            "action": "create_folder",
            "arguments": {"path": "/tmp/reports"},
            "expires_in_seconds": 300,
        },
    }


async def test_pending_approval_blocks_microphone_and_keeps_token_private():
    voice = service()
    await voice._on_result(confirmation())
    review = voice.pending_review
    assert review.action == "create_folder"
    assert "/tmp/reports" in review.arguments
    assert "private-approval-token" not in repr(review)
    assert "private-approval-token" not in repr(voice._approval)
    assert not voice.start(once=True)
    assert voice.status.state == VoiceState.APPROVAL


async def test_confirmation_rejects_stale_review_and_expiry():
    voice = service()
    await voice._on_result(confirmation())
    assert not voice.confirm_review("stale", True)
    review = voice.pending_review
    voice._approval = replace(voice._approval, view=replace(review, expires_at=0))
    assert voice.pending_review is None
    assert not voice.confirm_review(review.review_id, True)
    assert "expired" in voice.status.message


async def test_confirmation_submits_exact_token_once_and_does_not_listen(monkeypatch):
    voice = service()
    await voice._on_result(confirmation())
    review = voice.pending_review
    call = AsyncMock(return_value={"status": "completed", "message": "Created"})
    monkeypatch.setattr(LocalAPIAgent, "confirm", call)
    assert voice.confirm_review(review.review_id, True)
    assert not voice.confirm_review(review.review_id, True)
    assert await asyncio.to_thread(voice.wait, 2)
    call.assert_awaited_once_with("private-approval-token", True)
    assert voice.status.result_status == "completed"
    assert voice.pending_review is None
    assert voice._voice is None


def fake_carbon(register_code=0):
    def install(_target, _handler, _count, _type, _data, output):
        ct.cast(output, ct.POINTER(ct.c_void_p))[0] = ct.c_void_p(12)
        return 0

    def register(_key, _mods, _id, _target, _options, output):
        if not register_code:
            ct.cast(output, ct.POINTER(ct.c_void_p))[0] = ct.c_void_p(13)
        return register_code

    def event(_event, _name, _type, _actual, _size, _actual_size, output):
        ct.cast(output, ct.POINTER(HotKeyID))[0] = HotKeyID(int.from_bytes(b"Brdg", "big"), 1)
        return 0

    return SimpleNamespace(
        GetApplicationEventTarget=Mock(return_value=1),
        InstallEventHandler=Mock(side_effect=install),
        RegisterEventHotKey=Mock(side_effect=register),
        GetEventParameter=Mock(side_effect=event),
        UnregisterEventHotKey=Mock(),
        RemoveEventHandler=Mock(),
    )


def test_hotkey_routes_only_registered_key_and_unregisters(monkeypatch):
    monkeypatch.setattr("app.desktop.hotkey.sys.platform", "darwin")
    callback, lib = Mock(), fake_carbon()
    key = GlobalVoiceShortcut(callback, lib)
    assert key.start()
    assert key.handler(None, None, None) == 0
    callback.assert_called_once_with()
    assert lib.RegisterEventHotKey.call_args.args[:2] == (49, 768)
    key.close()
    key.close()
    lib.UnregisterEventHotKey.assert_called_once()
    lib.RemoveEventHandler.assert_called_once()


def test_hotkey_conflict_releases_handler_and_keeps_panel_available(monkeypatch):
    monkeypatch.setattr("app.desktop.hotkey.sys.platform", "darwin")
    lib = fake_carbon(register_code=-9878)
    key = GlobalVoiceShortcut(Mock(), lib)
    assert not key.start()
    assert "use Speak now" in key.status
    lib.RemoveEventHandler.assert_called_once()
    assert key.handler is None


async def test_silent_clip_never_reaches_transcription_or_agent():
    from app.voice.service import VoiceService

    stt, agent = SimpleNamespace(transcribe=AsyncMock()), SimpleNamespace(message=AsyncMock())
    recorder = SimpleNamespace(
        record_wav=AsyncMock(side_effect=NoSpeechDetected("No speech heard"))
    )
    voice = VoiceService(
        agent,
        Settings(_env_file=None, voice_start_sound=False),
        recorder=recorder,
        stt=stt,
        tts=SimpleNamespace(speak=AsyncMock()),
    )
    assert (await voice.listen_once())["status"] == "empty"
    stt.transcribe.assert_not_called()
    agent.message.assert_not_called()


async def test_stop_during_cue_does_not_open_microphone():
    from app.voice.service import VoiceService

    recorder = SimpleNamespace(record_wav=AsyncMock())
    voice = VoiceService(
        SimpleNamespace(message=AsyncMock()),
        Settings(_env_file=None),
        recorder=recorder,
        stt=SimpleNamespace(transcribe=AsyncMock()),
        tts=SimpleNamespace(speak=AsyncMock()),
    )

    async def cue():
        voice.stop()

    voice.cue = SimpleNamespace(play=cue)
    assert (await voice.listen_once())["status"] == "cancelled"
    recorder.record_wav.assert_not_called()


async def test_voice_training_records_retries_quiet_takes_and_installs(tmp_path, monkeypatch):
    import wave

    import numpy as np

    from app.desktop import voice_training

    monkeypatch.setattr(voice_training, "VOICE", tmp_path / "voice")
    installed = []
    monkeypatch.setattr(
        voice_training, "install_wake_word_model", lambda path: installed.append(path) or path
    )
    levels = iter([9000, 100, 9000, 9000, 9000, 9000, 9000, 50, 50])

    class Recorder:
        async def record_wav(self, seconds):
            path = tmp_path / f"clip-{len(list(tmp_path.glob('clip-*')))}.wav"
            samples = np.full(1600, next(levels), dtype=np.int16)
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(samples.tobytes())
            return path

    updates = []
    model = tmp_path / "model.onnx"
    model.write_bytes(b"onnx")
    training = AsyncMock(return_value=model)
    result = await voice_training.train(
        lambda state, message: updates.append((state, message)),
        threading.Event(),
        recorder=Recorder(),
        cue=SimpleNamespace(play=AsyncMock()),
        run_training=training,
    )
    assert result == model and installed == [model]
    assert len(list((tmp_path / "voice/phrase").glob("*.wav"))) == 6
    assert len(list((tmp_path / "voice/background").glob("*.wav"))) == 2
    messages = [message for _, message in updates]
    assert any("louder" in message for message in messages)
    assert "ready" in messages[-1]


def test_voice_training_explains_missing_tools(monkeypatch):
    from app.desktop import voice_training

    monkeypatch.setattr(voice_training.sys, "frozen", True, raising=False)
    assert "developer version" in voice_training.training_unavailable()
