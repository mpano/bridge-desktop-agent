from __future__ import annotations

import asyncio
import contextlib
import math
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
        if not math.isfinite(seconds) or not 0 < seconds <= 30:
            raise ValueError("Recording duration must be between 0 and 30 seconds.")
        worker = asyncio.create_task(asyncio.to_thread(self._record_wav, seconds))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Cancelling to_thread does not stop its microphone operation. Keep ownership
            # until the bounded recording closes, then remove its otherwise orphaned WAV.
            while not worker.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(worker)
            if not worker.cancelled() and worker.exception() is None:
                with contextlib.suppress(OSError):
                    worker.result().unlink()
            raise

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
        finally:
            # PortAudio failures can leave the convenience recording stream active.
            # Explicitly close it on both success and failure before handing off audio.
            with contextlib.suppress(Exception):
                sd.stop()
        samples = np.asarray(data, dtype=np.int16)
        with NamedTemporaryFile(prefix="bridge-", suffix=".wav", delete=False) as tmp:
            path = Path(tmp.name)
        try:
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(self.config.channels)
                wav.setsampwidth(2)
                wav.setframerate(self.config.sample_rate)
                wav.writeframes(samples.tobytes())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path
