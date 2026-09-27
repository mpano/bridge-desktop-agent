"""Build a local Bridge.app launcher pointing to this checkout and Python interpreter."""

import argparse
import importlib.util
import os
import plistlib
import shlex
import shutil
import sys
from pathlib import Path


def build_launcher(project: Path, python: Path, destination: Path) -> Path:
    project = project.expanduser().resolve()
    python = Path(os.path.abspath(python.expanduser()))
    destination = destination.expanduser().absolute()
    if not (project / "app/main.py").is_file():
        raise ValueError("Project directory must contain app/main.py.")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("Python executable does not exist or is not executable.")
    if destination.suffix != ".app":
        raise ValueError("Destination must end in .app.")

    icon_source = Path(__file__).with_name("assets") / "bridge-app-icon.png"
    if not icon_source.is_file():
        raise ValueError("Bridge application icon is missing from app/desktop/assets.")

    destination.mkdir(parents=True, exist_ok=False)
    contents = destination / "Contents"
    executable_directory = contents / "MacOS"
    resources = contents / "Resources"
    executable_directory.mkdir(parents=True)
    resources.mkdir(parents=True)
    shutil.copyfile(icon_source, resources / "BridgeIcon.png")

    metadata = {
        "CFBundleName": "Bridge",
        "CFBundleDisplayName": "Bridge",
        "CFBundleIdentifier": "app.bridge.desktop-agent",
        "CFBundleVersion": "1",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "Bridge",
        "CFBundleIconFile": "BridgeIcon.png",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSAppleEventsUsageDescription": (
            "Bridge controls applications only when you request an approved action."
        ),
        "NSMicrophoneUsageDescription": (
            "Bridge uses the microphone only for explicitly started voice commands "
            "and wake-word listening."
        ),
    }
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(metadata, stream)

    launcher = executable_directory / "Bridge"
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
    parser = argparse.ArgumentParser(description="Build a local Bridge .app launcher")
    parser.add_argument("--output", type=Path, default=Path("dist/Bridge.app"))
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("The Bridge app launcher requires macOS.")
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
