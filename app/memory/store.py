import os
import sqlite3
from pathlib import Path
from typing import Protocol

from app.memory.models import Preference
from app.security.permissions import checked_path


class MemoryRepository(Protocol):
    def get(self, key: str) -> Preference | None: ...
    def set(self, preference: Preference) -> None: ...
    def projects(self) -> list[Preference]: ...
    def delete_project(self, name: str) -> bool: ...
    def set_app_preferences(self, values: dict[str, str]) -> None: ...


class SQLiteMemory:
    """Store only typed application preferences and project aliases, never arbitrary secrets."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT)")
        os.chmod(path, 0o600)

    def get(self, key):
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT key, value FROM preferences WHERE key = ?", (key,)).fetchone()
        return Preference(*row) if row else None

    def set(self, preference):
        if preference.key in {"default_browser", "default_editor"}:
            from app.tools.system.apps import AppInput

            AppInput(app_name=preference.value)
        elif preference.key.startswith("project:"):
            checked_path(preference.value)
        else:
            raise ValueError("Only application preferences and project paths may be stored.")
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO preferences VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                "SET value=excluded.value",
                (preference.key, preference.value),
            )

    def projects(self) -> list[Preference]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT key,value FROM preferences WHERE key LIKE 'project:%' ORDER BY key"
            ).fetchall()
        return [Preference(*row) for row in rows]

    def delete_project(self, name: str) -> bool:
        with sqlite3.connect(self.path) as db:
            result = db.execute("DELETE FROM preferences WHERE key=?", ("project:" + name,))
            return result.rowcount > 0

    def set_app_preferences(self, values: dict[str, str]) -> None:
        from app.tools.system.apps import AppInput

        if set(values) != {"default_browser", "default_editor"}:
            raise ValueError("Only default browser and editor can be changed.")
        for value in values.values():
            AppInput(app_name=value)
        with sqlite3.connect(self.path) as db:
            db.executemany(
                "INSERT INTO preferences VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                "SET value=excluded.value",
                values.items(),
            )
