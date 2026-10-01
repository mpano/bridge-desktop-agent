"""Change a few settings in .env from the Settings screen, keeping everything else as is.

Only listed keys can be written. Comments, order and every other line (secrets included)
are left untouched; the file is replaced atomically and stays private (0600).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def update_env(path: Path, changes: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    pending = dict(changes)
    for index in range(len(lines) - 1, -1, -1):  # The last assignment wins in .env files.
        key = lines[index].split("=", 1)[0].strip()
        if key in pending and not lines[index].lstrip().startswith("#"):
            lines[index] = f"{key}={pending.pop(key)}"
    lines += [f"{key}={value}" for key, value in pending.items()]
    text = "\n".join(lines) + "\n"
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".env.")
    try:
        with os.fdopen(handle, "w") as file:
            file.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
