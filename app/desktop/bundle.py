"""Build a local .app launcher pointing to this checkout and its Python interpreter."""

import argparse
import importlib.util
import os
import plistlib
import shlex
import sys
from pathlib import Path


def build_launcher(project: Path, python: Path, destination: Path) -> Path:
    project = project.expanduser().resolve()
    # Preserve the venv executable path, not its symlink target (which loses site-packages).
    python = Path(os.path.abspath(python.expanduser()))
    destination = destination.expanduser().absolute()
    if not (project / "app/main.py").is_file():
        raise ValueError("Project directory must contain app/main.py.")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("Python executable does not exist or is not executable.")
    if destination.suffix != ".app":
        raise ValueError("Destination must end in .app.")
    destination.mkdir(parents=True, exist_ok=False)
    contents = destination / "Contents"
    executable_directory = contents / "MacOS"
    executable_directory.mkdir(parents=True)
    metadata = {
        "CFBundleName": "Desktop Agent",
        "CFBundleDisplayName": "Desktop Agent",
        "CFBundleIdentifier": "local.desktop-agent.launcher",
        "CFBundleVersion": "1",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "DesktopAgent",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSAppleEventsUsageDescription": (
            "Desktop Agent controls applications through registered tools."
        ),
    }
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(metadata, stream)
    launcher = executable_directory / "DesktopAgent"
    launcher.write_text(
        "#!/bin/sh\n"
        "# Local development launcher. Paths only; never copy .env or embed credentials here.\n"
        f"cd {shlex.quote(str(project))} || exit 1\n"
        f"exec {shlex.quote(str(python))} -m app.main --menubar\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a local Desktop Agent .app launcher")
    parser.add_argument("--output", type=Path, default=Path("dist/Desktop Agent.app"))
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("The app launcher requires macOS.")
    if importlib.util.find_spec("rumps") is None:
        parser.error("Install the optional dependency first: python -m pip install -e '.[menubar]'")
    project = Path(__file__).resolve().parents[2]
    try:
        target = build_launcher(project, Path(sys.executable), args.output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Created {target}\nThis launcher requires the current checkout and Python environment.")


if __name__ == "__main__":
    main()
