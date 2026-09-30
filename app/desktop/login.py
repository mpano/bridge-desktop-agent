"""Opt-in "Open at Login" via a per-user LaunchAgent that opens the installed Bridge.app."""

import os
import plistlib
from pathlib import Path

LABEL = "app.bridge.desktop-agent.login"


def running_app_bundle() -> Path | None:
    """The Bridge.app that launched this process, as exported by its launcher script."""
    value = os.environ.get("BRIDGE_APP_BUNDLE", "")
    path = Path(value) if value else None
    if path is None or path.suffix != ".app" or not path.is_dir():
        return None
    return path


class LoginItem:
    def __init__(self, app_bundle: Path | None, agents_directory: Path | None = None):
        self.app_bundle = app_bundle
        self.plist = (agents_directory or Path.home() / "Library/LaunchAgents") / f"{LABEL}.plist"

    @property
    def available(self) -> bool:
        return self.app_bundle is not None

    @property
    def enabled(self) -> bool:
        if not self.plist.is_file():
            return False
        try:
            arguments = plistlib.loads(self.plist.read_bytes()).get("ProgramArguments", [])
        except (OSError, plistlib.InvalidFileException):
            return False
        return self.app_bundle is not None and str(self.app_bundle) in arguments

    def set_enabled(self, enabled: bool) -> None:
        if not enabled:
            self.plist.unlink(missing_ok=True)
            return
        if self.app_bundle is None:
            raise ValueError("Open at Login is available when Bridge runs from Bridge.app.")
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        definition = {
            "Label": LABEL,
            # Opening the bundle keeps macOS privacy prompts attributed to Bridge.app.
            # --login keeps Bridge quietly in the menu bar instead of opening its panel.
            "ProgramArguments": ["/usr/bin/open", "-a", str(self.app_bundle), "--args", "--login"],
            "RunAtLoad": True,
            "LimitLoadToSessionType": "Aqua",
        }
        temporary = self.plist.with_suffix(".tmp")
        temporary.write_bytes(plistlib.dumps(definition))
        temporary.replace(self.plist)
