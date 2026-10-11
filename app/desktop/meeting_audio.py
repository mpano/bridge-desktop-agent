"""Hearing a meeting: your microphone ("you") and the Mac's sound ("others").

The microphone comes through sounddevice. The call (Zoom, Meet, Teams, a browser…) is the
Mac's own sound output, captured with ScreenCaptureKit's audio (it needs Screen Recording;
Bridge's own sounds are left out). Both arrive as 16 kHz mono and are cut into pieces at
natural pauses, so each piece is a few sentences; silence is dropped. Nothing is written to
disk here: each piece goes straight to the transcriber and is then forgotten.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

log = logging.getLogger(__name__)
RATE = 16000
BLOCK = 1600  # 0.1 s
SPEECH_LEVEL = 0.006  # RMS above this is sound worth keeping.
PAUSE = 0.9  # A pause this long ends a piece…
MIN_PIECE = 4.0  # …once it's at least this long.
MAX_PIECE = 25.0  # Never longer than this.


class Chunker:
    """Collects audio for one source and hands over a piece at each natural pause."""

    def __init__(self, source: str, emit):
        self.source, self.emit = source, emit
        self.parts: list[np.ndarray] = []
        self.started: float | None = None
        self.voiced = 0.0
        self.quiet = 0.0
        self.lock = threading.Lock()

    def feed(self, samples: np.ndarray, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if samples.size == 0:
            return
        level = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
        seconds = samples.size / RATE
        with self.lock:
            if self.started is None:
                if level < SPEECH_LEVEL:
                    return  # Silence before anyone speaks: drop it.
                self.started = now - seconds
            self.parts.append(samples.astype(np.float32, copy=False))
            if level >= SPEECH_LEVEL:
                self.voiced += seconds
                self.quiet = 0.0
            else:
                self.quiet += seconds
            length = now - self.started
            if (self.quiet >= PAUSE and length >= MIN_PIECE) or length >= MAX_PIECE:
                self._flush(now)

    def _flush(self, now: float) -> None:
        if self.parts and self.voiced >= 0.6:
            piece = np.concatenate(self.parts)
            self.emit(self.source, self.started, now, piece)
        self.parts, self.started, self.voiced, self.quiet = [], None, 0.0, 0.0

    def flush(self) -> None:
        with self.lock:
            self._flush(time.time())


class MeetingAudio:
    def __init__(self):
        self.callback = None
        self.mic = None
        self.stream = None
        self.output = None
        self.chunks: dict[str, Chunker] = {}

    def start(self, callback) -> str:
        """Start listening. Returns a warning if the call's sound can't be heard."""
        self.callback = callback
        self.chunks = {
            "you": Chunker("you", callback),
            "others": Chunker("others", callback),
        }
        self._start_mic()
        try:
            self._start_system()
        except Exception as exc:
            log.warning("Meeting notes: couldn't capture the Mac's sound: %s", exc)
            return (
                "Only your microphone can be heard, so the others' words come through only "
                "if your speakers are loud enough. Allow Screen Recording for Bridge to hear "
                "the call directly."
            )
        return ""

    def _start_mic(self) -> None:
        import sounddevice as sd

        chunker = self.chunks["you"]

        def heard(indata, _frames, _time, _status):
            chunker.feed(indata[:, 0].copy())

        self.mic = sd.InputStream(
            samplerate=RATE, channels=1, dtype="float32", blocksize=BLOCK, callback=heard
        )
        self.mic.start()

    def _start_system(self) -> None:
        import CoreMedia as CM
        import ScreenCaptureKit as SC

        chunker = self.chunks["others"]
        ready = threading.Event()
        found: dict = {}

        def got_content(content, error):
            found["content"], found["error"] = content, error
            ready.set()

        SC.SCShareableContent.getShareableContentWithCompletionHandler_(got_content)
        if not ready.wait(5) or found.get("content") is None:
            raise RuntimeError(str(found.get("error") or "No screen content (Screen Recording?)"))
        display = found["content"].displays()[0]
        content_filter = SC.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])
        config = SC.SCStreamConfiguration.alloc().init()
        config.setCapturesAudio_(True)
        config.setExcludesCurrentProcessAudio_(True)
        config.setSampleRate_(RATE)
        config.setChannelCount_(1)
        # Audio is what's wanted; keep the (required) video tiny and rare.
        config.setWidth_(2)
        config.setHeight_(2)
        config.setMinimumFrameInterval_(CM.CMTimeMake(1, 1))

        self.output = sound_receiver(chunker)
        self.stream = SC.SCStream.alloc().initWithFilter_configuration_delegate_(
            content_filter, config, None
        )
        # No queue of our own: ScreenCaptureKit delivers on one of its own.
        ok, error = self.stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self.output, SC.SCStreamOutputTypeAudio, None, None
        )
        if not ok:
            raise RuntimeError(str(error))
        started = threading.Event()
        problem: dict = {}

        def did_start(error):
            problem["error"] = error
            started.set()

        self.stream.startCaptureWithCompletionHandler_(did_start)
        if not started.wait(5) or problem.get("error") is not None:
            raise RuntimeError(str(problem.get("error") or "Capture didn't start"))

    def stop(self) -> None:
        for chunker in self.chunks.values():
            chunker.flush()
        if self.mic is not None:
            try:
                self.mic.stop()
                self.mic.close()
            except Exception:
                pass
            self.mic = None
        if self.stream is not None:
            done = threading.Event()
            try:
                self.stream.stopCaptureWithCompletionHandler_(lambda _error: done.set())
                done.wait(3)
            except Exception:
                pass
            self.stream = None


_Receiver = None


def sound_receiver(chunker: Chunker):
    """An object ScreenCaptureKit hands the Mac's sound to. The class is made once (Objective-C
    classes are global), and each meeting gets its own instance."""
    global _Receiver
    if _Receiver is None:
        from Foundation import NSObject

        class BridgeMeetingSound(NSObject):
            def stream_didOutputSampleBuffer_ofType_(self, _stream, buffer, kind):
                if kind != 1:  # SCStreamOutputTypeAudio
                    return
                try:
                    self.chunker.feed(samples_from(buffer))
                except Exception:
                    log.debug("Meeting notes: skipped an audio buffer.", exc_info=True)

        _Receiver = BridgeMeetingSound
    receiver = _Receiver.alloc().init()
    receiver.chunker = chunker
    return receiver


def samples_from(buffer) -> np.ndarray:
    """A CMSampleBuffer of 32-bit float mono audio as numpy samples."""
    import CoreMedia as CM

    block = CM.CMSampleBufferGetDataBuffer(buffer)
    if block is None:
        return np.zeros(0, dtype=np.float32)
    length = CM.CMBlockBufferGetDataLength(block)
    status, data = CM.CMBlockBufferCopyDataBytes(block, 0, length, None)
    if status != 0 or not data:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(bytes(data), dtype=np.float32).copy()
