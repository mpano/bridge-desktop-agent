from __future__ import annotations

import contextlib
import threading
from collections.abc import Awaitable, Callable
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
        on_result: Callable[[dict], Awaitable[dict]] | None = None,
    ):
        self.agent = agent
        self.settings = settings
        self.recorder = recorder or MicrophoneRecorder()
        self.stt = stt or self._build_stt()
        self.tts = tts or MacOSSayTTS(settings.voice_tts_voice, settings.voice_tts_rate)
        self.stop_event = threading.Event()
        self.on_result = on_result

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
            allow_download=self.settings.voice_allow_model_download,
        )

    async def listen_once(self) -> dict:
        if self.stop_event.is_set():
            return {"status": "cancelled", "message": "Voice service stopped."}
        path = await self.recorder.record_wav(self.settings.voice_record_seconds)
        try:
            transcript = await self.stt.transcribe(path)
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
        if self.stop_event.is_set():
            return {"status": "cancelled", "message": "Voice service stopped."}
        transcript = transcript.strip()
        if not transcript:
            return {"status": "empty", "transcript": "", "message": "I did not hear a command."}
        if len(transcript) > 10000:
            raise RuntimeError("Voice transcript exceeds the command length limit.")
        result = await self.agent.message(transcript)
        if self.on_result is not None:
            result = await self.on_result(result)
        if result["status"] == "confirmation_required":
            spoken = "That action needs confirmation. Please review it in Bridge."
        else:
            spoken = result.get("message") or "Done."
        response = {"status": result["status"], "transcript": transcript, "result": result}
        try:
            await self.tts.speak(spoken)
        except RuntimeError:
            # The action already happened. A speech failure must not imply it should be retried.
            response["speech_error"] = "Speech output failed; review the recorded action result."
        return response

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
            with contextlib.suppress(RuntimeError):
                await self.tts.speak("Yes?")
            try:
                result = await self.listen_once()
                if result["status"] == "confirmation_required":
                    # The request is now visible in Bridge's task/workflow UI. Never
                    # approve from spoken input; keep listening for the next wake word.
                    continue
                if result.get("speech_error"):
                    print(result["speech_error"])
            except RuntimeError:
                # Configuration or capture failures need attention, not an endless retry loop.
                self.stop()
                raise

    def stop(self) -> None:
        self.stop_event.set()

    async def close(self) -> None:
        self.stop()
        if hasattr(self.stt, "close"):
            await self.stt.close()
