import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.voice.service import VoiceService


class FakeRecorder:
    async def record_wav(self, seconds: float) -> Path:
        path = Path("/tmp/bridge-test-voice.wav")
        path.write_bytes(b"RIFF-test")
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
