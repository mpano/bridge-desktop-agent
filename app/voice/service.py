from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from app.voice.audio import MicrophoneRecorder
from app.voice.stt import LocalWhisperSTT, OpenAIWhisperSTT, SpeechToText
from app.voice.tts import MacOSSayTTS, TextToSpeech
from app.voice.wakeword import OpenWakeWordDetector


class VoiceService:
    """Routes spoken commands through the same Agent used by CLI/API."""

    def __init__(
        self,
        agent,
        settings,
        recorder: MicrophoneRecorder | None = None,
        stt: SpeechToText | None = None,
        tts: TextToSpeech | None = None,
    ):
        self.agent = agent
        self.settings = settings
        self.recorder = recorder or MicrophoneRecorder()
        self.stt = stt or self._build_stt()
        self.tts = tts or MacOSSayTTS(settings.voice_tts_voice, settings.voice_tts_rate)
        self.stop_event = asyncio.Event()

    def _build_stt(self) -> SpeechToText:
        if self.settings.voice_stt_provider == "openai":
            return OpenAIWhisperSTT(
                self.settings.openai_api_key.get_secret_value(),
                model=self.settings.voice_openai_stt_model,
                language=self.settings.voice_language,
            )
        return LocalWhisperSTT(
            model=self.settings.voice_local_whisper_model,
            language=self.settings.voice_language,
        )

    async def listen_once(self) -> dict:
        path = await self.recorder.record_wav(self.settings.voice_record_seconds)
        try:
            transcript = await self.stt.transcribe(path)
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
        if not transcript:
            return {"status": "empty", "transcript": "", "message": "I did not hear a command."}
        result = await self.agent.message(transcript)
        if result["status"] == "confirmation_required":
            spoken = "That action needs confirmation. Please review it in Bridge."
        else:
            spoken = result.get("message") or "Done."
        await self.tts.speak(spoken)
        return {"status": result["status"], "transcript": transcript, "result": result}

    async def run_background(self) -> None:
        if not self.settings.voice_background_enabled:
            raise RuntimeError(
                "Background voice service is disabled. Set VOICE_BACKGROUND_ENABLED=true "
                "to enable it explicitly."
            )
        if not self.settings.voice_wake_word_enabled:
            raise RuntimeError(
                "Background listening requires VOICE_WAKE_WORD_ENABLED=true so Bridge does "
                "not continuously transcribe ambient microphone audio."
            )
        model_path = self.settings.voice_wake_word_model_path
        if model_path is None:
            raise RuntimeError("Set VOICE_WAKE_WORD_MODEL_PATH to an openWakeWord model file.")
        detector = OpenWakeWordDetector(
            Path(model_path), threshold=self.settings.voice_wake_word_threshold
        )
        while not self.stop_event.is_set():
            await detector.wait(self.stop_event)
            if self.stop_event.is_set():
                break
            await self.tts.speak("Yes?")
            try:
                await self.listen_once()
            except Exception:
                await self.tts.speak("I could not process that command.")

    def stop(self) -> None:
        self.stop_event.set()
