"""Bounded local silence detection; audio is never uploaded to decide when to stop."""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field

from app.voice.audio import MicrophoneRecorder
from app.voice.errors import VoiceError

log = logging.getLogger(__name__)


class NoSpeechDetected(VoiceError):
    """No sustained audio activity; do not send silence to a transcription provider."""


CALIBRATION_SECONDS = 0.3
QUIETEST_SPEECH, LOUDEST_THRESHOLD = 0.004, 0.05


@dataclass
class SilenceEndpoint:
    """Decides when a recording ends. The first 0.3 s measure the room (and the mic's
    level), so speech counts as speech on a quiet mic and noise doesn't on a loud one."""

    silence_seconds: float = 1.0
    wait_seconds: float = 5.0
    threshold: float = 0.012
    elapsed: float = 0.0
    voiced: float = 0.0
    quiet: float = 0.0
    heard_speech: bool = False
    peak: float = 0.0
    noise: float | None = None
    total_voiced: float = 0.0
    adapt: bool = True
    _samples: list = field(default_factory=list)

    def _calibrate(self, rms: float) -> None:
        self._samples.append(rms)
        if self.elapsed >= CALIBRATION_SECONDS:
            # The quieter end of the window, in case you started talking straight away.
            ordered = sorted(self._samples)
            self.noise = ordered[len(ordered) // 5]
            self.threshold = min(max(self.noise * 3.0, QUIETEST_SPEECH), LOUDEST_THRESHOLD)

    def feed(self, rms: float, seconds: float) -> bool:
        """True ends the clip. Short clicks do not count as a spoken command."""
        if not math.isfinite(rms) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Invalid audio activity sample.")
        self.elapsed += seconds
        self.peak = max(self.peak, rms)
        if self.adapt and self.noise is None:
            self._calibrate(rms)
        if rms >= self.threshold:
            self.total_voiced += seconds
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
        self.last: dict = {}  # What the last recording looked like (levels, never audio).

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
        overflows = 0
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
                    overflows += bool(overflowed)  # A busy Mac drops a little audio; keep going.
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
        self.last = {
            "seconds": round(samples / 16000, 2),
            "noise": round(endpoint.noise or 0.0, 4),
            "threshold": round(endpoint.threshold, 4),
            "peak": round(endpoint.peak, 4),
            "voiced": round(endpoint.total_voiced, 2),
            "overflows": overflows,
        }
        log.info("Recording: %s", self.last)
        if self.stop.is_set():
            raise NoSpeechDetected("Recording stopped.")
        if samples and endpoint.peak < 0.0005:
            raise NoSpeechDetected(
                "The microphone is silent. Check the input device in System Settings › Sound, "
                "and that Bridge is allowed to use the Microphone."
            )
        if not endpoint.heard_speech:
            raise NoSpeechDetected(
                "I did not hear a command. Try speaking again after the start sound."
            )
        return self._write_wav(np.concatenate(chunks))
