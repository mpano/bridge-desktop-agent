"""Run the user's Apple Shortcuts. Untrusted shortcuts need approval; trusted ones run directly."""

import tempfile
from pathlib import Path

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool

SHORTCUTS = "/usr/bin/shortcuts"
Name = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")


class ShortcutListInput(Input):
    query: str | None = Field(default=None, max_length=100, description="Optional name filter")


class ShortcutRunInput(Input):
    name: str = Name
    input: str | None = Field(
        default=None, max_length=20000, description="Optional text passed to the shortcut"
    )


class ShortcutsController:
    def __init__(self, runner, trusted: list[str] | tuple[str, ...] = ()):
        self.runner = runner
        self.trusted = {name.casefold() for name in trusted}

    async def names(self) -> list[str]:
        output = await self.runner.run(SHORTCUTS, "list")
        return [line.strip() for line in output.splitlines() if line.strip()]

    async def list(self, args):
        names = await self.names()
        if args.query:
            names = [name for name in names if args.query.casefold() in name.casefold()]
        return {"shortcuts": names[:200], "total": len(names)}

    def policy(self, args) -> RiskLevel:
        # Shortcuts can send messages or change files; only user-trusted ones skip approval.
        return RiskLevel.SAFE if args.name.casefold() in self.trusted else RiskLevel.CONFIRM

    async def run(self, args):
        available = {name.casefold(): name for name in await self.names()}
        name = available.get(args.name.casefold())
        if name is None:
            raise ValueError(f"No shortcut named “{args.name}”. Check the Shortcuts app.")
        with tempfile.TemporaryDirectory(prefix="bridge-shortcut-") as scratch:
            output = Path(scratch) / "output.txt"
            argv = [SHORTCUTS, "run", name, "--output-path", str(output)]
            argv += ["--output-type", "public.plain-text"]
            if args.input is not None:
                source = Path(scratch) / "input.txt"
                source.write_text(args.input, encoding="utf-8")
                argv += ["--input-path", str(source)]
            await self.runner.run(*argv)
            text = output.read_text(encoding="utf-8", errors="replace") if output.exists() else ""
        return {
            "shortcut": name,
            "output": text[:8000],
            "output_truncated": len(text) > 8000,
            "message": f"Ran the “{name}” shortcut.",
        }


def render_list(data: dict) -> str:
    if not data["shortcuts"]:
        return "No shortcuts found. Create one in the Shortcuts app, then ask again."
    return "Your shortcuts:\n" + "\n".join("• " + name for name in data["shortcuts"])


def render_run(data: dict) -> str:
    output = data["output"].strip()
    return f"✓ {data['message']}" + (f"\n{output}" if output else "")


def register(registry, controller):
    registry.register(
        Tool(
            "shortcuts_list",
            "List the user's Apple Shortcuts, optionally filtered by name.",
            ShortcutListInput,
            RiskLevel.SAFE,
            controller.list,
            render=render_list,
        )
    )
    registry.register(
        Tool(
            "shortcuts_run",
            "Run one of the user's Apple Shortcuts by exact name, optionally with text input, "
            "and return its text output. Use shortcuts_list to find names.",
            ShortcutRunInput,
            RiskLevel.SAFE,
            controller.run,
            policy=controller.policy,
            persist_arguments=False,
            confirmation_message="Run this shortcut? It can perform any action it was built "
            "for. Add it to SHORTCUTS_TRUSTED in .env to skip this approval.",
            render=render_run,
        )
    )
