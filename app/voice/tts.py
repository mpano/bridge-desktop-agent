from __future__ import annotations

from typing import Protocol

from app.tools.macos.applescript import NativeRunner


class TextToSpeech(Protocol):
    async def speak(self, text: str) -> None: ...


class MacOSSayTTS:
    """Local speech through a bounded native subprocess, with text on stdin."""

    def __init__(
        self,
        voice: str | None = None,
        rate: int | None = None,
        *,
        runner: NativeRunner | None = None,
    ):
        self.voice = voice
        self.rate = rate
        self.runner = runner or NativeRunner()

    async def speak(self, text: str) -> None:
        if not text.strip():
            return
        args = ["/usr/bin/say"]
        if self.voice:
            args += ["-v", self.voice]
        if self.rate is not None:
            args += ["-r", str(self.rate)]
        try:
            await self.runner.run(*args, input_text=text[:4000])
        except (RuntimeError, OSError, TimeoutError) as exc:
            raise RuntimeError(
                "Text-to-speech failed or timed out; check macOS speech settings."
            ) from exc
