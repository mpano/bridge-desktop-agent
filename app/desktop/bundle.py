"""Build a local Bridge.app launcher pointing to this checkout and Python interpreter."""

import argparse
import importlib.util
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

BUNDLE_ID = "app.bridge.desktop-agent"
VERSION = "0.1.0"
LOG_FILE = "~/Library/Logs/Bridge/bridge.log"


def _launcher_script(project: Path, python: Path) -> str:
    missing = (
        'display alert "Bridge cannot start" message "Its Python environment or project '
        'folder moved. Rebuild the app with: python -m app.desktop.bundle --force"'
    )
    return (
        "#!/bin/sh\n"
        "# Local development launcher. Paths only; never copy .env or embed credentials here.\n"
        f"PROJECT={shlex.quote(str(project))}\n"
        f"PYTHON={shlex.quote(str(python))}\n"
        'BRIDGE_APP_BUNDLE="$(cd "$(dirname "$0")/../.." && pwd)"\n'
        "export BRIDGE_APP_BUNDLE\n"
        '[ "$1" = "--login" ] && export BRIDGE_LAUNCHED_AT_LOGIN=1\n'
        'if [ ! -x "$PYTHON" ] || [ ! -f "$PROJECT/app/main.py" ]; then\n'
        f"  /usr/bin/osascript -e {shlex.quote(missing)} >/dev/null 2>&1\n"
        "  exit 1\n"
        "fi\n"
        f'LOG="{LOG_FILE.replace("~", "$HOME")}"\n'
        'mkdir -p "$(dirname "$LOG")"\n'
        # Keep the previous run for diagnostics; logs contain status, never credentials.
        '[ -f "$LOG" ] && mv -f "$LOG" "$LOG.1"\n'
        'cd "$PROJECT" || exit 1\n'
        'exec "$PYTHON" -m app.main --menubar >>"$LOG" 2>&1\n'
    )


def _write_icon(resources: Path) -> str:
    """Prefer a crisp multi-resolution .icns rendered from the vector mark."""
    try:
        from app.desktop.icon import render_icns

        render_icns(resources / "Bridge.icns")
        return "Bridge.icns"
    except (ImportError, OSError, subprocess.CalledProcessError):
        source = Path(__file__).with_name("assets") / "bridge-app-icon.png"
        if not source.is_file():
            raise ValueError(
                "Bridge application icon is missing from app/desktop/assets."
            ) from None
        shutil.copyfile(source, resources / "BridgeIcon.png")
        return "BridgeIcon.png"


def info_plist(icon: str, executable: str = "Bridge") -> dict:
    """Info.plist shared by the dev launcher and the self-contained app."""
    return {
        "CFBundleName": "Bridge",
        "CFBundleDisplayName": "Bridge",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": executable,
        "CFBundleIconFile": icon,
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "LSMinimumSystemVersion": "13.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Bridge — a local macOS desktop agent.",
        "NSAppleEventsUsageDescription": (
            "Bridge controls applications only when you request an approved action."
        ),
        "NSCalendarsFullAccessUsageDescription": (
            "Bridge reads and adds calendar events when you ask about your schedule."
        ),
        "NSMicrophoneUsageDescription": (
            "Bridge uses the microphone only for explicitly started voice commands "
            "and wake-word listening."
        ),
    }


def _is_bridge_bundle(path: Path) -> bool:
    try:
        info = plistlib.loads((path / "Contents/Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return False
    return info.get("CFBundleIdentifier") == BUNDLE_ID


def build_launcher(
    project: Path,
    python: Path,
    destination: Path,
    *,
    replace: bool = False,
    sign: bool = False,
) -> Path:
    project = project.expanduser().resolve()
    python = Path(os.path.abspath(python.expanduser()))
    destination = destination.expanduser().absolute()
    if not (project / "app/main.py").is_file():
        raise ValueError("Project directory must contain app/main.py.")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("Python executable does not exist or is not executable.")
    if destination.suffix != ".app":
        raise ValueError("Destination must end in .app.")
    if replace and destination.exists():
        # Only ever replace a bundle this builder generated.
        if not _is_bridge_bundle(destination):
            raise ValueError(f"{destination} exists and is not a Bridge build; not replacing it.")
        shutil.rmtree(destination)

    destination.mkdir(parents=True, exist_ok=False)
    try:
        contents = destination / "Contents"
        executable_directory = contents / "MacOS"
        resources = contents / "Resources"
        executable_directory.mkdir(parents=True)
        resources.mkdir(parents=True)
        icon = _write_icon(resources)

        metadata = info_plist(icon)
        with (contents / "Info.plist").open("wb") as stream:
            plistlib.dump(metadata, stream)
        (contents / "PkgInfo").write_text("APPL????", encoding="ascii")

        launcher = executable_directory / "Bridge"
        launcher.write_text(_launcher_script(project, python), encoding="utf-8")
        launcher.chmod(0o755)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise

    if sign:
        _ad_hoc_sign(destination)
    return destination


def _ad_hoc_sign(bundle: Path) -> bool:
    """A stable local signature lets macOS remember privacy grants for Bridge.app."""
    codesign = shutil.which("codesign")
    if codesign is None:
        return False
    result = subprocess.run(
        [codesign, "--force", "--sign", "-", str(bundle)], capture_output=True, check=False
    )
    return result.returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a local Bridge .app launcher")
    parser.add_argument("--output", type=Path, default=Path("dist/Bridge.app"))
    parser.add_argument(
        "--install",
        action="store_true",
        help="Build into ~/Applications/Bridge.app so it appears in Launchpad and Spotlight",
    )
    parser.add_argument(
        "--force", action="store_true", help="Replace an existing Bridge build at the destination"
    )
    parser.add_argument("--no-sign", action="store_true", help="Skip ad-hoc code signing")
    parser.add_argument("--open", action="store_true", help="Launch Bridge after building")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("The Bridge app launcher requires macOS.")
    if importlib.util.find_spec("rumps") is None:
        parser.error("Install the optional dependency first: python -m pip install -e '.[menubar]'")
    project = Path(__file__).resolve().parents[2]
    output = Path.home() / "Applications/Bridge.app" if args.install else args.output
    try:
        target = build_launcher(
            project, Path(sys.executable), output, replace=args.force, sign=not args.no_sign
        )
    except FileExistsError:
        parser.error(f"{output} already exists. Pass --force to rebuild it.")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(
        f"Created {target}\n"
        "This launcher uses the current checkout and Python environment.\n"
        f"Logs: {LOG_FILE}"
    )
    if args.open:
        subprocess.run(["/usr/bin/open", str(target)], check=False)


if __name__ == "__main__":
    main()
