from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol


class SpeechToText(Protocol):
    async def transcribe(self, audio_path: Path) -> str: ...


class LocalWhisperSTT:
    """Local transcription using faster-whisper. No audio leaves the Mac."""

    def __init__(self, model: str = "small", language: str | None = "en"):
        self.model_name = model
        self.language = language
        self._model = None

    def _get_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    "Local STT requires the optional voice dependencies: "
                    "python -m pip install -e '.[voice]'"
                ) from exc
            self._model = WhisperModel(self.model_name, device="auto", compute_type="int8")
        return self._model

    async def transcribe(self, audio_path: Path) -> str:
        return await asyncio.to_thread(self._transcribe, audio_path)

    def _transcribe(self, audio_path: Path) -> str:
        model = self._get_model()
        segments, _ = model.transcribe(
            str(audio_path),
            language=self.language or None,
            vad_filter=True,
        )
        return " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()


class OpenAIWhisperSTT:
    """Remote transcription. Audio is uploaded only when explicitly configured."""

    def __init__(self, api_key: str, model: str = "gpt-4o-mini-transcribe", language: str | None = "en"):
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for remote voice transcription.")
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.language = language

    async def transcribe(self, audio_path: Path) -> str:
        with audio_path.open("rb") as audio:
            result = await self.client.audio.transcriptions.create(
                model=self.model,
                file=audio,
                language=self.language or None,
            )
        return result.text.strip()
