import asyncio
import logging
from collections.abc import Callable

from app.tools.macos.applescript import NativeRunner

log = logging.getLogger(__name__)
SOUND = "/System/Library/Sounds/Tink.aiff"


def play_native_sound() -> float | None:
    """Play the cue in-process (instant); return its length, or None if AppKit is missing.

    Launching afplay takes 1–5 s while audio devices (e.g. headphones) wake up, which
    delayed or broke recording. NSSound plays immediately without a new process.
    """
    try:
        import AppKit as AK
    except ImportError:
        return None
    sound = AK.NSSound.alloc().initWithContentsOfFile_byReference_(SOUND, True)
    if sound is None or not sound.play():
        return None
    play_native_sound.current = sound  # Keep a reference until it finishes playing.
    return float(sound.duration())


class StartCue:
    """A short "go ahead" sound. It is a courtesy: if it can't play, recording continues."""

    def __init__(
        self,
        runner: NativeRunner | None = None,
        native: Callable[[], float | None] | None = play_native_sound,
    ):
        self.runner = runner or NativeRunner()
        self.native = native

    async def play(self) -> None:
        duration = self.native() if self.native is not None else None
        if duration is not None:
            # Wait for the sound to finish so it isn't captured in the command recording.
            await asyncio.sleep(min(duration, 1.0) + 0.1)
            return
        try:
            async with asyncio.timeout(6):
                await self.runner.run("/usr/bin/afplay", SOUND)
            await asyncio.sleep(0.1)
        except (RuntimeError, OSError, TimeoutError):
            log.warning("Start sound could not play; recording without it.")
