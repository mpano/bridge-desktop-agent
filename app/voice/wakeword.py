from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Callable
from pathlib import Path
from threading import Event

from app.voice.errors import VoiceError


class OpenWakeWordDetector:
    """Local ONNX detector; models must be installed explicitly, never downloaded here."""

    def __init__(
        self,
        model_path: Path,
        threshold: float = 0.5,
        sample_rate: int = 16000,
        on_ready: Callable[[], None] | None = None,
    ):
        model_path = model_path.expanduser().resolve()
        if not model_path.is_file() or model_path.suffix.lower() != ".onnx":
            raise VoiceError("Wake-word model must be an existing .onnx file.")
        if not math.isfinite(threshold) or not 0 < threshold <= 1:
            raise ValueError("Wake-word threshold must be between 0 and 1.")
        if sample_rate != 16000:
            raise ValueError("Wake-word detection requires 16000 Hz audio.")
        self.model_path = model_path
        self.threshold = threshold
        self.sample_rate = sample_rate
        self.on_ready = on_ready
        self._model = None

    async def wait(self, stop_event: Event) -> None:
        worker = asyncio.create_task(asyncio.to_thread(self._wait_blocking, stop_event))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            stop_event.set()
            while not worker.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(worker)
            if not worker.cancelled():
                worker.exception()  # Retrieve errors without leaking device details.
            raise

    def _wait_blocking(self, stop_event: Event) -> None:
        if stop_event.is_set():
            return
        try:
            import numpy as np
            import openwakeword
            import sounddevice as sd
            from openwakeword.model import Model
        except ImportError as exc:
            raise VoiceError(
                "Wake word support requires the optional voice dependencies: "
                "python -m pip install -e '.[voice,wakeword]'"
            ) from exc
        shared_models = Path(openwakeword.__file__).parent / "resources" / "models"
        if any(
            not (shared_models / name).is_file()
            for name in ("melspectrogram.onnx", "embedding_model.onnx")
        ):
            raise VoiceError(
                "Install openWakeWord shared ONNX assets as documented in docs/VOICE.md."
            )
        try:
            if self._model is None:
                self._model = Model(
                    wakeword_models=[str(self.model_path)], inference_framework="onnx"
                )
            else:
                self._model.reset()
            model = self._model
        except Exception as exc:
            raise VoiceError(
                "Wake-word model could not load. Choose an openWakeWord-compatible ONNX model "
                "and check its shared assets."
            ) from exc
        try:
            block_size = 1280
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=block_size,
            ) as stream:
                if self.on_ready is not None and not stop_event.is_set():
                    self.on_ready()
                # Poll availability so a quiet/disconnected device cannot trap shutdown
                # in an unbounded blocking read.
                while not stop_event.wait(0.02):
                    if stream.read_available < block_size:
                        continue
                    audio, _ = stream.read(block_size)
                    scores = model.predict(np.asarray(audio).reshape(-1))
                    if scores and max(float(value) for value in scores.values()) >= self.threshold:
                        return
        except Exception as exc:
            raise VoiceError(
                "Wake-word capture failed. Check the local ONNX model and macOS "
                "Microphone permission for the application running Bridge."
            ) from exc
