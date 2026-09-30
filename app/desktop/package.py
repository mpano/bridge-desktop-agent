"""Build a self-contained Bridge.app and a .dmg installer with PyInstaller.

    python -m app.desktop.package            # dist/standalone/Bridge.app + dist/Bridge-<v>.dmg
    python -m app.desktop.package --install  # also copy the app into ~/Applications

Unlike app.desktop.bundle, this app needs neither this checkout nor the virtual
environment. Settings live in ~/Library/Application Support/Bridge/.env. The build is
ad-hoc signed for this Mac; sharing it widely needs a Developer ID and notarization.
"""

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from app.desktop.bundle import BUNDLE_ID, VERSION, _is_bridge_bundle, info_plist

PROJECT = Path(__file__).resolve().parents[2]
DATA = [
    ("app/ui/static", "app/ui/static"),
    ("app/desktop/assets", "app/desktop/assets"),
    ("docs", "docs"),
    (".env.example", "."),
]
# Large optional pieces that no Bridge feature uses at runtime.
EXCLUDE = ["playwright", "tkinter", "onnx", "sklearn", "scipy", "matplotlib", "pytest", "IPython"]
COLLECT_DATA = ["faster_whisper", "openwakeword"]
HIDDEN = ["keyring.backends.macOS", "EventKit", "uvicorn.lifespan.on", "uvicorn.loops.asyncio"]


def pyinstaller_args(icon: Path, dist: Path, work: Path) -> list[str]:
    args = [
        str(PROJECT / "app/desktop/standalone.py"),
        "--name=Bridge",
        "--windowed",
        "--noconfirm",
        "--clean",
        f"--icon={icon}",
        f"--osx-bundle-identifier={BUNDLE_ID}",
        f"--distpath={dist}",
        f"--workpath={work}",
        f"--specpath={work}",
        f"--paths={PROJECT}",
        "--collect-submodules=app",
        "--collect-submodules=uvicorn",
    ]
    for source, target in DATA:
        args.append(f"--add-data={PROJECT / source}{os.pathsep}{target}")
    args += [f"--collect-data={name}" for name in COLLECT_DATA]
    args += [f"--hidden-import={name}" for name in HIDDEN]
    args += [f"--exclude-module={name}" for name in EXCLUDE]
    return args


def finish_bundle(app: Path) -> None:
    """Add Bridge's usage descriptions and menu-bar-only flag, then re-sign."""
    plist = app / "Contents/Info.plist"
    info = plistlib.loads(plist.read_bytes())
    info.update(info_plist(info.get("CFBundleIconFile", "Bridge.icns"), info["CFBundleExecutable"]))
    plist.write_bytes(plistlib.dumps(info))
    subprocess.run(
        ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)],
        check=True,
        capture_output=True,
    )


def make_dmg(app: Path, output: Path) -> Path:
    output.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as staging:
        shutil.copytree(app, Path(staging) / app.name, symlinks=True)
        (Path(staging) / "Applications").symlink_to("/Applications")
        subprocess.run(
            [
                "/usr/bin/hdiutil",
                "create",
                "-volname",
                "Bridge",
                "-srcfolder",
                staging,
                "-format",
                "UDZO",
                str(output),
            ],
            check=True,
            capture_output=True,
        )
    return output


def install(app: Path, applications: Path) -> Path:
    target = applications / "Bridge.app"
    if target.exists():
        if not _is_bridge_bundle(target):
            raise ValueError(f"{target} exists and is not a Bridge build; not replacing it.")
        shutil.rmtree(target)
    applications.mkdir(parents=True, exist_ok=True)
    shutil.copytree(app, target, symlinks=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the self-contained Bridge.app")
    parser.add_argument("--install", action="store_true", help="Copy into ~/Applications")
    parser.add_argument("--no-dmg", action="store_true", help="Skip the .dmg installer")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Bridge.app can only be built on macOS.")
    try:
        import PyInstaller.__main__ as pyinstaller
    except ImportError:
        parser.error("Install the packager first: python -m pip install -e '.[package]'")

    from app.desktop.icon import render_icns

    dist, work = PROJECT / "dist/standalone", PROJECT / "build/pyinstaller"
    work.mkdir(parents=True, exist_ok=True)
    icon = render_icns(work / "Bridge.icns")
    pyinstaller.run(pyinstaller_args(icon, dist, work))
    app = dist / "Bridge.app"
    finish_bundle(app)
    print(f"Built {app}")
    if not args.no_dmg:
        print(f"Installer {make_dmg(app, PROJECT / f'dist/Bridge-{VERSION}.dmg')}")
    if args.install:
        print(f"Installed {install(app, Path.home() / 'Applications')}")


if __name__ == "__main__":
    main()
