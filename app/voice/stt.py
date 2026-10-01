from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from app.voice.errors import VoiceError


def build_stt(settings) -> SpeechToText:
    if settings.voice_stt_provider == "openai":
        return OpenAIWhisperSTT(
            settings.openai_api_key.get_secret_value(),
            model=settings.voice_openai_stt_model,
            language=settings.voice_language,
        )
    return LocalWhisperSTT(
        model=settings.voice_local_whisper_model,
        language=settings.voice_language,
        allow_download=settings.voice_allow_model_download,
    )


class SpeechToText(Protocol):
    async def transcribe(self, audio_path: Path) -> str: ...

    async def close(self) -> None: ...


class LocalWhisperSTT:
    """Offline transcription; downloading missing weights requires explicit opt-in."""

    def __init__(
        self,
        model: str = "small",
        language: str | None = "en",
        *,
        allow_download: bool = False,
    ):
        self.model_name = model
        self.language = language
        self.allow_download = allow_download
        self._model = None
        self._lock = asyncio.Lock()
        self._closed = False

    def _get_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise VoiceError(
                    "Local STT requires the optional voice dependencies: "
                    "python -m pip install -e '.[voice]'"
                ) from None
            try:
                self._model = WhisperModel(
                    self.model_name,
                    device="auto",
                    compute_type="int8",
                    local_files_only=not self.allow_download,
                )
            except Exception:
                raise VoiceError(
                    "Local speech model could not load. Install cached model weights or explicitly "
                    "enable VOICE_ALLOW_MODEL_DOWNLOAD for initial setup."
                ) from None
        return self._model

    async def transcribe(self, audio_path: Path) -> str:
        async with self._lock:
            if self._closed:
                raise VoiceError("Speech transcription is closed.")
            worker = asyncio.create_task(asyncio.to_thread(self._transcribe, audio_path))
            cancelled = False
            # Python cannot cancel an inference thread. Keep the caller's temporary audio
            # alive until the worker exits, including on repeated cancellation requests.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            if cancelled:
                if not worker.cancelled():
                    worker.exception()
                raise asyncio.CancelledError
            return worker.result()

    def _transcribe(self, audio_path: Path) -> str:
        model = self._get_model()
        try:
            segments, _ = model.transcribe(
                str(audio_path), language=self.language or None, vad_filter=True
            )
            return " ".join(s.text.strip() for s in segments if s.text.strip()).strip()
        except Exception:
            raise VoiceError(
                "Local speech transcription failed. Check the audio and model."
            ) from None

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            self._model = None


class OpenAIWhisperSTT:
    """Remote transcription. Audio is uploaded only when explicitly configured."""

    def __init__(self, api_key: str, model: str = "gpt-transcribe", language: str | None = "en"):
        if not api_key:
            raise VoiceError("OPENAI_API_KEY is required for remote voice transcription.")
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key, timeout=60.0, max_retries=0)
        self.model = model
        self.language = language
        self._closed = False
        self._lock = asyncio.Lock()

    async def transcribe(self, audio_path: Path) -> str:
        async with self._lock:
            if self._closed:
                raise VoiceError("Speech transcription is closed.")
            try:
                with audio_path.open("rb") as audio:
                    arguments = {"model": self.model, "file": audio}
                    if self.language:
                        arguments["language"] = self.language
                    result = await self.client.audio.transcriptions.create(**arguments)
                return result.text.strip()
            except Exception:
                raise VoiceError(
                    "Remote speech transcription failed. Check network access, API key, and model."
                ) from None

    async def close(self) -> None:
        async with self._lock:
            if not self._closed:
                self._closed = True
                await self.client.close()
