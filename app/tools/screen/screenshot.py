import os
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class AccessibilityController(Protocol):
    async def inspect_tree(self) -> dict: ...


class ComputerVisionController(Protocol):
    async def understand(self, image: Path) -> dict: ...


class ScreenController:
    def __init__(self, runner, directory: Path):
        self.runner, self.directory = runner, directory

    async def take_screenshot(self, args):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self.directory / f"{uuid4().hex}.png"
        try:
            await self.runner.run("/usr/sbin/screencapture", "-x", str(destination))
            if not destination.is_file() or destination.stat().st_size == 0:
                raise RuntimeError("Screenshot unavailable. Enable Screen Recording permission.")
            os.chmod(destination, 0o600)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return {"path": str(destination), "message": "Screenshot saved locally."}


def register(registry, controller):
    registry.register(
        Tool(
            "take_screenshot",
            "Save a screenshot locally; image is not sent to the LLM.",
            Input,
            RiskLevel.SAFE,
            controller.take_screenshot,
        )
    )
