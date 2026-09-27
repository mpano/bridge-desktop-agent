from __future__ import annotations

import asyncio
from pathlib import Path


class OpenWakeWordDetector:
    """Optional local wake-word detector backed by an explicitly supplied model file."""

    def __init__(self, model_path: Path, threshold: float = 0.5, sample_rate: int = 16000):
        if not model_path.exists():
            raise RuntimeError(f"Wake-word model not found: {model_path}")
        self.model_path = model_path
        self.threshold = threshold
        self.sample_rate = sample_rate

    async def wait(self, stop_event: asyncio.Event) -> None:
        await asyncio.to_thread(self._wait_blocking, stop_event)

    def _wait_blocking(self, stop_event: asyncio.Event) -> None:
        try:
            import numpy as np
            import sounddevice as sd
            from openwakeword.model import Model
        except ImportError as exc:
            raise RuntimeError(
                "Wake word support requires the optional voice dependencies: "
                "python -m pip install -e '.[voice]'"
            ) from exc

        model = Model(wakeword_models=[str(self.model_path)])
        block_size = 1280
        with sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=block_size,
        ) as stream:
            while not stop_event.is_set():
                audio, _ = stream.read(block_size)
                scores = model.predict(np.asarray(audio).reshape(-1))
                if scores and max(float(value) for value in scores.values()) >= self.threshold:
                    return
