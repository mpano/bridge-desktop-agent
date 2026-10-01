from __future__ import annotations

import contextlib
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.voice.audio import MicrophoneRecorder
from app.voice.cue import StartCue
from app.voice.endpointing import EndpointingRecorder, NoSpeechDetected
from app.voice.errors import VoiceError
from app.voice.paths import wake_word_model_path
from app.voice.stt import SpeechToText, build_stt
from app.voice.tts import MacOSSayTTS, TextToSpeech
from app.voice.wakeword import OpenWakeWordDetector


@dataclass(frozen=True)
class VoiceEvent:
    phase: str
    message: str
    transcript: str | None = None


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
        on_event: Callable[[VoiceEvent], None] | None = None,
        cue: StartCue | None = None,
    ):
        self.agent = agent
        self.settings = settings
        self.stop_event = threading.Event()
        self.recorder = recorder or (
            EndpointingRecorder(
                self.stop_event,
                silence_seconds=settings.voice_silence_seconds,
                wait_seconds=settings.voice_speech_wait_seconds,
                threshold=settings.voice_activity_threshold,
            )
            if getattr(settings, "voice_auto_stop", False)
            else MicrophoneRecorder()
        )
        self.cue = cue or StartCue()
        self.stt = stt or self._build_stt()
        self.tts = tts or MacOSSayTTS(settings.voice_tts_voice, settings.voice_tts_rate)
        self.on_result = on_result
        self.on_event = on_event

    def _emit(self, phase: str, message: str, transcript: str | None = None) -> None:
        if self.on_event is not None:
            self.on_event(VoiceEvent(phase, message, transcript))

    def _build_stt(self) -> SpeechToText:
        return build_stt(self.settings)

    async def listen_once(self) -> dict:
        if self.stop_event.is_set():
            return {"status": "cancelled", "message": "Voice service stopped."}
        if getattr(self.settings, "voice_start_sound", False):
            self._emit("starting", "Get ready — speak after the start sound.")
            await self.cue.play()
            if self.stop_event.is_set():
                return {"status": "cancelled", "message": "Voice service stopped."}
        self._emit("recording", f"Speak now · up to {self.settings.voice_record_seconds:g}s")
        try:
            path = await self.recorder.record_wav(self.settings.voice_record_seconds)
        except NoSpeechDetected as exc:
            if self.stop_event.is_set():
                return {"status": "cancelled", "message": "Voice service stopped."}
            self._emit("ready", str(exc), "")
            return {"status": "empty", "transcript": "", "message": str(exc)}
        try:
            if self.stop_event.is_set():
                return {"status": "cancelled", "message": "Voice service stopped."}
            self._emit("transcribing", "Transcribing your command…")
            transcript = await self.stt.transcribe(path)
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
        if self.stop_event.is_set():
            return {"status": "cancelled", "message": "Voice service stopped."}
        transcript = transcript.strip()
        if not transcript:
            self._emit("ready", "I did not hear a command. Try speaking again.", "")
            return {"status": "empty", "transcript": "", "message": "I did not hear a command."}
        if len(transcript) > 10000:
            raise VoiceError("Voice transcript exceeds the command length limit.")
        self._emit("processing", "Working on your command…", transcript)
        result = await self.agent.message(transcript)
        if self.on_result is not None:
            result = await self.on_result(result)
        if result["status"] == "confirmation_required":
            spoken = "That action needs confirmation. Please review it in Bridge."
        else:
            spoken = result.get("message") or "Done."
        response = {"status": result["status"], "transcript": transcript, "result": result}
        if self.stop_event.is_set():
            return response
        self._emit("speaking", spoken)
        try:
            await self.tts.speak(spoken)
        except RuntimeError:
            # The action already happened. A speech failure must not imply it should be retried.
            response["speech_error"] = "Speech output failed; review the recorded action result."
        return response

    async def run_background(self) -> None:
        if not self.settings.voice_background_enabled:
            raise VoiceError(
                "Background voice service is disabled. Set VOICE_BACKGROUND_ENABLED=true "
                "to enable it explicitly."
            )
        if not self.settings.voice_wake_word_enabled:
            raise VoiceError(
                "Background listening requires VOICE_WAKE_WORD_ENABLED=true so Bridge does "
                "not continuously transcribe ambient microphone audio."
            )
        model_path = wake_word_model_path(self.settings.voice_wake_word_model_path)
        if not model_path.is_file():
            raise VoiceError(
                'Install the custom "Hey Bridge" wake-word model with '
                '"bridge --install-wakeword /path/to/hey_bridge.onnx".'
            )
        detector = OpenWakeWordDetector(
            model_path,
            threshold=self.settings.voice_wake_word_threshold,
            on_ready=lambda: self._emit("listening", "Waiting for your wake phrase…"),
        )
        while not self.stop_event.is_set():
            self._emit("starting", "Preparing wake-word detector…")
            await detector.wait(self.stop_event)
            if self.stop_event.is_set():
                break
            if not getattr(self.settings, "voice_start_sound", False):
                with contextlib.suppress(RuntimeError):
                    self._emit("speaking", "Wake word detected. Wait for ‘Yes?’ then speak.")
                    await self.tts.speak("Yes?")
            try:
                result = await self.listen_once()
                if result["status"] == "confirmation_required":
                    # Pause until a human reviews the action in the panel or dashboard.
                    # Restarting the listener is an explicit user action.
                    return
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
