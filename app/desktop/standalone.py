"""Entry point for the self-contained Bridge.app (built by app.desktop.package).

A packaged app has no project folder, so settings (.env), the database and screenshots
live in ~/Library/Application Support/Bridge, and logs in ~/Library/Logs/Bridge.
"""

import os
import secrets
import subprocess
import sys
from pathlib import Path

SUPPORT = Path.home() / "Library/Application Support/Bridge"
LOGS = Path.home() / "Library/Logs/Bridge"


def resource(relative: str) -> Path:
    """Files shipped inside the app (PyInstaller unpacks them under sys._MEIPASS)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / relative


def ensure_settings(support: Path = SUPPORT) -> tuple[Path, bool]:
    """Create a private .env on first launch, with a fresh API_TOKEN. Returns (path, created)."""
    support.mkdir(parents=True, exist_ok=True)
    env = support / ".env"
    if env.exists():
        return env, False
    template = resource(".env.example")
    lines = template.read_text(encoding="utf-8").splitlines() if template.is_file() else []
    token = secrets.token_urlsafe(32)
    output, seen = [], False
    for line in lines:
        if line.startswith("API_TOKEN="):
            line, seen = f"API_TOKEN={token}", True
        elif line.startswith("DATABASE_PATH="):
            line = "DATABASE_PATH=./agent.db"
        output.append(line)
    if not seen:
        output.append(f"API_TOKEN={token}")
    descriptor = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(
            "# Bridge settings. Add your OPENAI_API_KEY, save, then\n"
            "# quit and reopen Bridge. This file stays on this Mac.\n" + "\n".join(output) + "\n"
        )
    return env, True


def main() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / "bridge.log"
    if log.exists():
        log.replace(LOGS / "bridge.log.1")
    stream = open(log, "a", buffering=1, encoding="utf-8")  # noqa: SIM115 - process lifetime
    sys.stdout = sys.stderr = stream

    env, created = ensure_settings()
    os.chdir(env.parent)
    if getattr(sys, "frozen", False):
        # Contents/MacOS/Bridge → the .app, for "Open at Login".
        os.environ["BRIDGE_APP_BUNDLE"] = str(Path(sys.executable).resolve().parents[2])
    if "--login" in sys.argv:
        os.environ["BRIDGE_LAUNCHED_AT_LOGIN"] = "1"
    if created and not os.environ.get("BRIDGE_SKIP_FIRST_RUN"):
        subprocess.run(["/usr/bin/open", "-e", str(env)], check=False)

    from app.config.settings import Settings
    from app.desktop.menubar import run_menubar

    run_menubar(Settings())


if __name__ == "__main__":
    main()
