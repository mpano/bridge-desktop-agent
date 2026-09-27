"""Explicitly approved text clipboard access using native macOS utilities."""

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.macos.applescript import NativeRunner
from app.tools.registry import ToolRegistry


class ClipboardText(Input):
    text: str = Field(min_length=1, max_length=8000)


class ClipboardController:
    def __init__(self, runner: NativeRunner):
        self.runner = runner

    async def read(self, _: Input) -> dict:
        text = await self.runner.run(
            "/usr/bin/pbpaste",
            "-Prefer",
            "txt",
            env={"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"},
            strip_output=False,
        )
        if len(text) > 8000:
            raise ValueError("Clipboard text exceeds 8,000 characters; no text was returned.")
        return {"text": text, "characters": len(text)}

    async def write(self, args: ClipboardText) -> dict:
        await self.runner.run(
            "/usr/bin/pbcopy",
            input_text=args.text,
            env={"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"},
        )
        return {"message": "Text copied to the clipboard.", "characters": len(args.text)}


def register(registry: ToolRegistry, controller: ClipboardController) -> None:
    registry.register(
        Tool(
            "read_clipboard",
            "Read up to 8,000 characters of clipboard text after approval. "
            "Use only when the user asks to read or process clipboard contents.",
            Input,
            RiskLevel.CONFIRM,
            controller.read,
            confirmation_message="Read the current clipboard? Its text enters local execution "
            "results; sharing with the model follows your configured privacy policy. "
            "Do not approve if it contains passwords, tokens, or other secrets.",
        )
    )
    registry.register(
        Tool(
            "copy_to_clipboard",
            "Replace clipboard contents with the specified text after approval.",
            ClipboardText,
            RiskLevel.CONFIRM,
            controller.write,
            confirmation_message="Replace the clipboard with this text? Previous clipboard "
            "contents will not be saved or restored.",
        )
    )
