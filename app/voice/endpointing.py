"""Bounded local silence detection; audio is never uploaded to decide when to stop."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

from app.voice.audio import MicrophoneRecorder
from app.voice.errors import VoiceError


class NoSpeechDetected(VoiceError):
    """No sustained audio activity; do not send silence to a transcription provider."""


@dataclass
class SilenceEndpoint:
    silence_seconds: float = 1.0
    wait_seconds: float = 5.0
    threshold: float = 0.012
    elapsed: float = 0.0
    voiced: float = 0.0
    quiet: float = 0.0
    heard_speech: bool = False

    def feed(self, rms: float, seconds: float) -> bool:
        """True ends the clip. Short clicks do not count as a spoken command."""
        if not math.isfinite(rms) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Invalid audio activity sample.")
        self.elapsed += seconds
        if rms >= self.threshold:
            self.voiced += seconds
            self.quiet = 0
            if self.voiced >= 0.2:
                self.heard_speech = True
        else:
            self.quiet += seconds
            if not self.heard_speech:
                self.voiced = 0
        return (self.heard_speech and self.quiet >= self.silence_seconds) or (
            not self.heard_speech and self.elapsed >= self.wait_seconds
        )


class EndpointingRecorder(MicrophoneRecorder):
    def __init__(
        self,
        stop: threading.Event,
        *,
        silence_seconds=1.0,
        wait_seconds=5.0,
        threshold=0.012,
        finish: threading.Event | None = None,
        max_seconds: float = 30.0,
    ):
        super().__init__()
        self.stop = stop
        # finish ends the recording early but keeps what was said; stop discards it.
        self.finish = finish or threading.Event()
        self.max_seconds = max_seconds
        self.silence_seconds = silence_seconds
        self.wait_seconds = wait_seconds
        self.threshold = threshold

    def cancel_recording(self) -> None:
        self.stop.set()

    def _record_wav(self, seconds: float):
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            raise VoiceError(
                "Install voice capture dependencies: pip install -e '.[voice]'"
            ) from exc
        endpoint = SilenceEndpoint(self.silence_seconds, self.wait_seconds, self.threshold)
        chunks = []
        samples = 0
        block = 320  # 20 ms at 16 kHz; all audio stays local until transcription.
        deadline = time.monotonic() + seconds
        try:
            with sd.InputStream(
                samplerate=16000, channels=1, dtype="int16", blocksize=block
            ) as stream:
                while (
                    not self.stop.is_set()
                    and not self.finish.is_set()
                    and time.monotonic() < deadline
                ):
                    if stream.read_available < block:
                        self.stop.wait(0.01)
                        continue
                    data, overflowed = stream.read(block)
                    if overflowed:
                        raise VoiceError("Microphone audio was interrupted. Try recording again.")
                    audio = np.asarray(data, dtype=np.int16).reshape(-1).copy()
                    chunks.append(audio)
                    samples += len(audio)
                    rms = float(np.sqrt(np.mean((audio.astype(np.float32) / 32768) ** 2)))
                    ended = endpoint.feed(rms, len(audio) / 16000)
                    if ended or samples >= int(seconds * 16000):
                        break
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError(
                "Microphone capture failed. Check Microphone permission and your input device."
            ) from exc
        if self.stop.is_set():
            raise NoSpeechDetected("Recording stopped.")
        if not endpoint.heard_speech:
            raise NoSpeechDetected(
                "I did not hear a command. Try speaking again after the start sound."
            )
        return self._write_wav(np.concatenate(chunks))
