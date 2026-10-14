"""Make a Bridge release anyone can open: signed, notarized by Apple, ready to auto-update.

    python -m app.desktop.release --notes "What's new"            # build, sign, notarize
    python -m app.desktop.release --notes "What's new" --publish  # … and put it on GitHub

What you need once (see docs/releasing.md):
  * a "Developer ID Application" certificate in your keychain (Apple Developer Program);
  * notarytool credentials saved under a profile name:
        xcrun notarytool store-credentials bridge-notary --apple-id … --team-id …
    (it asks for an app-specific password itself; Bridge never sees it).

It builds Bridge.app (app.desktop.package), signs every binary inside it with the hardened
runtime, sends it to Apple, staples Apple's ticket to the app, and writes to dist/release:
Bridge-<v>.dmg (to download), Bridge-<v>.zip (what Bridge's updater downloads) and
latest.json (what the updater reads). --publish makes the GitHub release v<v> with them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.desktop.bundle import VERSION

PROJECT = Path(__file__).resolve().parents[2]
REPO = "mpano/bridge-desktop-agent"
PROFILE = "bridge-notary"
# What Bridge does under the hardened runtime: Python needs writable+executable memory and
# its own libraries; the rest are the Mac features Bridge asks you about.
ENTITLEMENTS = {
    "com.apple.security.cs.allow-unsigned-executable-memory": True,
    "com.apple.security.cs.disable-library-validation": True,
    "com.apple.security.device.audio-input": True,
    "com.apple.security.automation.apple-events": True,
    "com.apple.security.personal-information.calendars": True,
    "com.apple.security.personal-information.addressbook": True,
}
MACHO = {
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
}


class ReleaseError(RuntimeError):
    pass


def developer_ids(output: str) -> list[tuple[str, str]]:
    """(full identity name, team id) for each Developer ID Application certificate."""
    found = re.findall(r'"(Developer ID Application: [^"]+ \(([A-Z0-9]{10})\))"', output)
    return list(dict.fromkeys(found))


def find_identity(run=subprocess.run) -> tuple[str, str]:
    result = run(
        ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
        capture_output=True,
        text=True,
        check=False,
    )
    found = developer_ids(result.stdout or "")
    if not found:
        raise ReleaseError(
            "No “Developer ID Application” certificate in your keychain. Join the Apple "
            "Developer Program and create one (docs/releasing.md, step 1)."
        )
    if len(found) > 1:
        raise ReleaseError(
            "Several Developer ID certificates: pass one with --identity.\n  "
            + "\n  ".join(name for name, _ in found)
        )
    return found[0]


def binaries(app: Path) -> list[Path]:
    """Every Mach-O file inside the app, deepest first (sign inside out)."""
    found = []
    for path in app.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                if stream.read(4) in MACHO:
                    found.append(path)
        except OSError:
            continue
    return sorted(found, key=lambda p: (-len(p.relative_to(app).parts), str(p)))


def sign(app: Path, identity: str, entitlements: Path, run=subprocess.run) -> None:
    base = ["/usr/bin/codesign", "--force", "--timestamp", "--options", "runtime"]
    for path in binaries(app):
        run([*base, "--sign", identity, str(path)], check=True, capture_output=True)
    run(
        [*base, "--entitlements", str(entitlements), "--sign", identity, str(app)],
        check=True,
        capture_output=True,
    )
    run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        check=True,
        capture_output=True,
    )


def notarize(path: Path, profile: str, run=subprocess.run) -> None:
    """Send to Apple and wait (usually a few minutes). Raises with Apple's log on failure."""
    result = run(
        [
            "/usr/bin/xcrun",
            "notarytool",
            "submit",
            str(path),
            "--keychain-profile",
            profile,
            "--wait",
            "--output-format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        answer = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        answer = {}
    if answer.get("status") == "Accepted":
        return
    detail = (result.stderr or result.stdout or "").strip()[-800:]
    if answer.get("id"):
        log = run(
            ["/usr/bin/xcrun", "notarytool", "log", answer["id"], "--keychain-profile", profile],
            capture_output=True,
            text=True,
            check=False,
        )
        detail = (log.stdout or detail)[-3000:]
    raise ReleaseError(f"Apple didn't notarize {path.name} ({answer.get('status')}):\n{detail}")


def staple(path: Path, run=subprocess.run) -> None:
    run(["/usr/bin/xcrun", "stapler", "staple", str(path)], check=True, capture_output=True)


def zip_app(app: Path, output: Path, run=subprocess.run) -> Path:
    output.unlink(missing_ok=True)
    run(
        ["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(output)],
        check=True,
        capture_output=True,
    )
    return output


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest(version: str, archive: Path, notes: str, repo: str = REPO) -> dict:
    return {
        "version": version,
        "url": f"https://github.com/{repo}/releases/download/v{version}/{archive.name}",
        "sha256": sha256(archive),
        "size": archive.stat().st_size,
        "notes": notes.strip(),
        "published": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build a signed, notarized Bridge release")
    parser.add_argument("--notes", default="", help="What's new, shown in Bridge's update alert")
    parser.add_argument("--identity", help="The Developer ID Application identity to sign with")
    parser.add_argument("--profile", default=PROFILE, help="notarytool keychain profile")
    parser.add_argument("--publish", action="store_true", help="Create the GitHub release")
    parser.add_argument("--skip-build", action="store_true", help="Reuse dist/standalone")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.error("Releases are made on a Mac.")
    try:
        identity = args.identity or find_identity()[0]
    except ReleaseError as exc:
        parser.exit(1, f"{exc}\n")
    out = PROJECT / "dist/release"
    out.mkdir(parents=True, exist_ok=True)
    app = PROJECT / "dist/standalone/Bridge.app"
    if not args.skip_build:
        print("Building Bridge.app…", flush=True)
        subprocess.run([sys.executable, "-m", "app.desktop.package", "--no-dmg"], check=True)
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleShortVersionString") != VERSION:
        parser.exit(1, "The built app's version isn't VERSION; build again.\n")

    try:
        entitlements = out / "entitlements.plist"
        entitlements.write_bytes(plistlib.dumps(ENTITLEMENTS))
        print(f"Signing with {identity}…", flush=True)
        sign(app, identity, entitlements)
        print("Sending the app to Apple for notarization (a few minutes)…", flush=True)
        upload = zip_app(app, out / "upload.zip")
        notarize(upload, args.profile)
        upload.unlink(missing_ok=True)
        staple(app)
        archive = zip_app(app, out / f"Bridge-{VERSION}.zip")  # Now with the ticket inside.

        from app.desktop.package import make_dmg

        dmg = make_dmg(app, out / f"Bridge-{VERSION}.dmg")
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--timestamp", "--sign", identity, str(dmg)],
            check=True,
            capture_output=True,
        )
        print("Notarizing the installer…", flush=True)
        notarize(dmg, args.profile)
        staple(dmg)
    except subprocess.CalledProcessError as exc:
        error = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        parser.exit(1, f"{' '.join(map(str, exc.cmd[:3]))} failed:\n{error}\n")
    except ReleaseError as exc:
        parser.exit(1, f"{exc}\n")

    latest = out / "latest.json"
    latest.write_text(json.dumps(manifest(VERSION, archive, args.notes), indent=2) + "\n")
    print(f"Ready in {out}: {dmg.name}, {archive.name}, latest.json")
    if args.publish:
        gh = shutil.which("gh")
        if gh is None:
            parser.exit(1, "Install the GitHub CLI (gh) to publish, or upload the files by hand.\n")
        subprocess.run(
            [
                gh,
                "release",
                "create",
                f"v{VERSION}",
                str(dmg),
                str(archive),
                str(latest),
                "--repo",
                REPO,
                "--title",
                f"Bridge {VERSION}",
                "--notes",
                args.notes or f"Bridge {VERSION}",
            ],
            check=True,
        )
        print(f"Published https://github.com/{REPO}/releases/tag/v{VERSION}")


if __name__ == "__main__":
    main()
