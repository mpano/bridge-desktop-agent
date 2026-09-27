from __future__ import annotations

import asyncio
import wave
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 16000
    channels: int = 1


class MicrophoneRecorder:
    """Small optional sounddevice boundary. Imports audio dependencies only when used."""

    def __init__(self, config: AudioConfig | None = None):
        self.config = config or AudioConfig()

    async def record_wav(self, seconds: float) -> Path:
        if seconds <= 0:
            raise ValueError("Recording duration must be positive.")
        return await asyncio.to_thread(self._record_wav, seconds)

    def _record_wav(self, seconds: float) -> Path:
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError(
                "Voice capture requires the optional voice dependencies: "
                "python -m pip install -e '.[voice]'"
            ) from exc

        frames = int(seconds * self.config.sample_rate)
        try:
            data = sd.rec(
                frames,
                samplerate=self.config.sample_rate,
                channels=self.config.channels,
                dtype="int16",
            )
            sd.wait()
        except Exception as exc:
            raise RuntimeError(
                "Microphone capture failed. Check System Settings > Privacy & Security > "
                "Microphone and allow the app or Terminal running Bridge."
            ) from exc
        samples = np.asarray(data, dtype=np.int16)
        with NamedTemporaryFile(prefix="bridge-", suffix=".wav", delete=False) as tmp:
            path = Path(tmp.name)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(self.config.channels)
            wav.setsampwidth(2)
            wav.setframerate(self.config.sample_rate)
            wav.writeframes(samples.tobytes())
        return path
