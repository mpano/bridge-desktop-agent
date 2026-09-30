from pathlib import Path
from tempfile import NamedTemporaryFile
from types import SimpleNamespace

import pytest

from app.voice.service import VoiceService


class FakeRecorder:
    async def record_wav(self, seconds: float) -> Path:
        with NamedTemporaryFile(suffix=".wav", delete=False) as audio:
            audio.write(b"RIFF-test")
            path = Path(audio.name)
        return path


class FakeSTT:
    def __init__(self, text: str):
        self.text = text

    async def transcribe(self, audio_path: Path) -> str:
        assert audio_path.exists()
        return self.text


class FakeTTS:
    def __init__(self):
        self.messages = []

    async def speak(self, text: str) -> None:
        self.messages.append(text)


class FakeAgent:
    def __init__(self, result: dict):
        self.result = result
        self.messages = []

    async def message(self, text: str) -> dict:
        self.messages.append(text)
        return self.result


def settings(**overrides):
    values = {
        "voice_stt_provider": "local",
        "voice_local_whisper_model": "small",
        "voice_openai_stt_model": "gpt-transcribe",
        "voice_language": "en",
        "voice_record_seconds": 2,
        "voice_tts_voice": None,
        "voice_tts_rate": None,
        "voice_background_enabled": False,
        "voice_wake_word_enabled": False,
        "voice_wake_word_model_path": None,
        "voice_wake_word_threshold": 0.5,
        "openai_api_key": SimpleNamespace(get_secret_value=lambda: ""),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_listen_once_routes_transcript_through_agent_and_speaks_result():
    agent = FakeAgent({"status": "completed", "message": "Spotify opened", "steps": []})
    tts = FakeTTS()
    service = VoiceService(
        agent,
        settings(),
        recorder=FakeRecorder(),
        stt=FakeSTT("open spotify"),
        tts=tts,
    )

    result = await service.listen_once()

    assert result["transcript"] == "open spotify"
    assert agent.messages == ["open spotify"]
    assert tts.messages == ["Spotify opened"]


@pytest.mark.asyncio
async def test_voice_never_auto_approves_confirmation():
    confirmation = {
        "status": "confirmation_required",
        "message": "Approval required",
        "steps": [],
        "confirmation": {"token": "secret", "arguments": {"path": "/tmp/example"}},
    }
    agent = FakeAgent(confirmation)
    tts = FakeTTS()
    service = VoiceService(
        agent,
        settings(),
        recorder=FakeRecorder(),
        stt=FakeSTT("delete the file"),
        tts=tts,
    )

    result = await service.listen_once()

    assert result["status"] == "confirmation_required"
    assert tts.messages == ["That action needs confirmation. Please review it in Bridge."]


@pytest.mark.asyncio
async def test_background_service_is_disabled_by_default():
    service = VoiceService(
        FakeAgent({}),
        settings(),
        recorder=FakeRecorder(),
        stt=FakeSTT(""),
        tts=FakeTTS(),
    )

    with pytest.raises(RuntimeError, match="disabled"):
        await service.run_background()


@pytest.mark.asyncio
async def test_background_service_requires_wake_word():
    service = VoiceService(
        FakeAgent({}),
        settings(voice_background_enabled=True),
        recorder=FakeRecorder(),
        stt=FakeSTT(""),
        tts=FakeTTS(),
    )

    with pytest.raises(RuntimeError, match="VOICE_WAKE_WORD_ENABLED"):
        await service.run_background()


async def test_typed_confirmation_uses_shared_presenter(monkeypatch):
    from unittest.mock import AsyncMock

    from app.main import present_result

    agent = FakeAgent(
        {
            "status": "confirmation_required",
            "message": "Allow it?",
            "steps": [],
            "confirmation": {"token": "token", "arguments": {"text": "hello"}},
        }
    )
    agent.confirm = AsyncMock(return_value={"status": "completed", "message": "Done", "steps": []})
    monkeypatch.setattr("app.main.read_input", AsyncMock(return_value="y"))
    service = VoiceService(
        agent,
        settings(),
        recorder=FakeRecorder(),
        stt=FakeSTT("copy hello"),
        tts=FakeTTS(),
        on_result=lambda result: present_result(agent, result),
    )
    assert (await service.listen_once())["status"] == "completed"
    agent.confirm.assert_awaited_once_with("token", True)


async def test_speech_failure_preserves_completed_action():
    from unittest.mock import AsyncMock

    agent = FakeAgent({"status": "completed", "message": "Done", "steps": []})
    service = VoiceService(
        agent,
        settings(),
        recorder=FakeRecorder(),
        stt=FakeSTT("open app"),
        tts=SimpleNamespace(speak=AsyncMock(side_effect=RuntimeError("failure"))),
    )
    result = await service.listen_once()
    assert result["status"] == "completed"
    assert result["speech_error"]
    assert agent.messages == ["open app"]


async def test_stop_after_transcription_prevents_execution():
    agent = FakeAgent({})
    service = VoiceService(
        agent, settings(), recorder=FakeRecorder(), stt=FakeSTT("open app"), tts=FakeTTS()
    )

    async def transcribe(path):
        service.stop()
        return "open app"

    service.stt.transcribe = transcribe
    assert (await service.listen_once())["status"] == "cancelled"
    assert not agent.messages


async def test_transcription_failure_removes_recording():
    class Recorder(FakeRecorder):
        async def record_wav(self, seconds):
            self.path = await super().record_wav(seconds)
            return self.path

    class FailingSTT:
        async def transcribe(self, path):
            raise RuntimeError("transcription failed")

    recorder = Recorder()
    service = VoiceService(
        FakeAgent({}), settings(), recorder=recorder, stt=FailingSTT(), tts=FakeTTS()
    )
    with pytest.raises(RuntimeError):
        await service.listen_once()
    assert not recorder.path.exists()


def test_blank_optional_voice_settings():
    from app.config.settings import Settings

    config = Settings(
        _env_file=None, voice_tts_rate="", voice_tts_voice="", voice_wake_word_model_path=""
    )
    assert config.voice_tts_rate is None
    assert config.voice_tts_voice is None
    assert config.voice_wake_word_model_path is None
    assert not config.voice_allow_model_download


async def test_background_routes_result_and_stops_without_repeating(tmp_path, monkeypatch):
    class Detector:
        def __init__(self, *args, **kwargs):
            pass

        async def wait(self, stop_event):
            pass

    monkeypatch.setattr("app.voice.service.OpenWakeWordDetector", Detector)
    agent = FakeAgent({"status": "completed", "message": "Done", "steps": []})
    delivered = []

    async def report(result):
        delivered.append(result)
        service.stop()
        return result

    model_path = tmp_path / "model.onnx"
    model_path.touch()
    service = VoiceService(
        agent,
        settings(
            voice_background_enabled=True,
            voice_wake_word_enabled=True,
            voice_wake_word_model_path=model_path,
        ),
        recorder=FakeRecorder(),
        stt=FakeSTT("open app"),
        tts=FakeTTS(),
        on_result=report,
    )
    await service.run_background()
    assert agent.messages == ["open app"]
    assert len(delivered) == 1
    assert service.stop_event.is_set()


async def test_approval_eof_declines(monkeypatch):
    from unittest.mock import AsyncMock

    from app.main import present_result

    agent = SimpleNamespace(
        confirm=AsyncMock(
            return_value={
                "status": "cancelled",
                "message": "Declined",
                "steps": [],
            }
        )
    )
    monkeypatch.setattr("app.main.read_input", AsyncMock(side_effect=EOFError))
    await present_result(
        agent,
        {
            "status": "confirmation_required",
            "message": "Approve?",
            "steps": [],
            "confirmation": {"token": "token", "arguments": {}},
        },
    )
    agent.confirm.assert_awaited_once_with("token", False)


async def test_background_pauses_after_pending_approval(tmp_path, monkeypatch):
    waits = []

    class Detector:
        def __init__(self, *args, **kwargs):
            self.ready = kwargs["on_ready"]

        async def wait(self, stop):
            waits.append(True)
            assert len(waits) == 1
            self.ready()

    monkeypatch.setattr("app.voice.service.OpenWakeWordDetector", Detector)
    model = tmp_path / "bridge.onnx"
    model.touch()
    events = []
    service = VoiceService(
        FakeAgent({"status": "confirmation_required", "message": "Approve?"}),
        settings(
            voice_background_enabled=True,
            voice_wake_word_enabled=True,
            voice_wake_word_model_path=model,
        ),
        recorder=FakeRecorder(),
        stt=FakeSTT("make a folder"),
        tts=FakeTTS(),
        on_event=events.append,
    )
    await service.run_background()
    phases = [event.phase for event in events]
    assert phases.index("starting") < phases.index("listening") < phases.index("recording")
    assert len(waits) == 1


async def test_stop_during_recording_skips_transcription():
    from unittest.mock import AsyncMock

    class Recorder(FakeRecorder):
        async def record_wav(self, seconds):
            self.path = await super().record_wav(seconds)
            service.stop()
            return self.path

    recorder = Recorder()
    stt = SimpleNamespace(transcribe=AsyncMock())
    service = VoiceService(FakeAgent({}), settings(), recorder=recorder, stt=stt, tts=FakeTTS())
    assert (await service.listen_once())["status"] == "cancelled"
    stt.transcribe.assert_not_called()
    assert not recorder.path.exists()
