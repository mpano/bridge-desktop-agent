"""Bounded metadata searches; never read file contents or follow symlinks."""

import asyncio
import os
import stat
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from app.security.permissions import PolicyError, checked_path
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.registry import ToolRegistry


class FileSearchInput(Input):
    scope: Literal["Downloads", "Desktop", "Documents"] = "Downloads"
    name_contains: str = Field(default="", max_length=200)
    extension: str = Field(default="", max_length=20)
    modified: Literal["any", "today", "yesterday"] = "any"
    recursive: bool = False
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("extension")
    @classmethod
    def extension_only(cls, value: str) -> str:
        value = value.removeprefix(".").lower()
        if value and not value.isalnum():
            raise ValueError("Use an extension such as pdf or txt.")
        return value


class FileSearchController:
    def __init__(self, home: Path | None = None, *, max_entries: int = 5000):
        self.home = home or Path.home()
        self.max_entries = max_entries

    async def search(self, args: FileSearchInput) -> dict:
        return await asyncio.to_thread(self._search, args)

    def _search(self, args: FileSearchInput) -> dict:
        root = self.home / args.scope
        if root.is_symlink():
            raise ValueError("Search scope cannot be a symbolic link.")
        root = checked_path(str(root))
        if not root.is_dir():
            raise ValueError("Search folder does not exist or is unavailable.")
        today = datetime.now().astimezone().date()
        target = today - timedelta(days=1) if args.modified == "yesterday" else today
        pending = [root]
        matches: list[dict] = []
        examined, skipped = 0, 0
        truncated = False
        deadline = time.monotonic() + 5
        while pending and not truncated:
            folder = pending.pop()
            try:
                # Recheck queued directories before traversal, including policy and scope.
                if folder.is_symlink() or not checked_path(str(folder)).is_relative_to(root):
                    skipped += 1
                    continue
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if examined >= self.max_entries or time.monotonic() >= deadline:
                            truncated = True
                            break
                        examined += 1
                        if entry.name.startswith(".") or entry.is_symlink():
                            continue
                        try:
                            path = checked_path(entry.path)
                            if not path.is_relative_to(root):
                                continue
                            metadata = entry.stat(follow_symlinks=False)
                            if stat.S_ISDIR(metadata.st_mode):
                                if args.recursive:
                                    pending.append(Path(entry.path))
                                continue
                            if not stat.S_ISREG(metadata.st_mode):
                                continue
                            if args.name_contains.casefold() not in entry.name.casefold():
                                continue
                            if args.extension and path.suffix.lower() != f".{args.extension}":
                                continue
                            modified = datetime.fromtimestamp(metadata.st_mtime).astimezone()
                            if args.modified != "any" and modified.date() != target:
                                continue
                            matches.append(
                                {
                                    "path": str(path),
                                    "name": entry.name,
                                    "size_bytes": metadata.st_size,
                                    "modified_at": modified.isoformat(),
                                }
                            )
                            if len(matches) >= args.limit:
                                truncated = True
                                break
                        except (OSError, ValueError):
                            skipped += 1
            except (OSError, PolicyError):
                if folder == root:
                    raise ValueError(
                        "Cannot search this folder. Check macOS Privacy & Security > "
                        "Files and Folders permissions for the launching app."
                    ) from None
                skipped += 1
        return {
            "scope": str(root),
            "files": sorted(matches, key=lambda item: item["path"]),
            "examined_entries": examined,
            "skipped_unavailable": skipped,
            "truncated": truncated,
            "date_basis": "Local modification date, not download date.",
            "message": "Metadata search finished. Results may be partial; "
            "file contents were not read.",
        }


def register(registry: ToolRegistry, controller: FileSearchController) -> None:
    registry.register(
        Tool(
            "find_files",
            "Find file metadata in Downloads, Desktop, or Documents after approval. "
            "Filter by filename substring, extension, or local modification date. "
            "Never reads contents; modification date does not prove download date.",
            FileSearchInput,
            RiskLevel.CONFIRM,
            controller.search,
        )
    )
