from __future__ import annotations

import asyncio
import shutil
from typing import Protocol


class TextToSpeech(Protocol):
    async def speak(self, text: str) -> None: ...


class MacOSSayTTS:
    """Local TTS through macOS 'say'. Text is passed as an argument, never through a shell."""

    def __init__(self, voice: str | None = None, rate: int | None = None):
        self.voice = voice
        self.rate = rate

    async def speak(self, text: str) -> None:
        if not text.strip():
            return
        executable = shutil.which("say")
        if not executable:
            raise RuntimeError("macOS 'say' is unavailable.")
        args = [executable]
        if self.voice:
            args += ["-v", self.voice]
        if self.rate:
            args += ["-r", str(self.rate)]
        args.append(text[:4000])
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError("Text-to-speech failed: " + stderr.decode(errors="replace")[:300])
