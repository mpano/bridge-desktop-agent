"""Finder selection and file metadata, without opening document contents."""

import asyncio
import stat
from datetime import datetime

from app.security.permissions import checked_path
from app.security.risk import RiskLevel
from app.tools.base import Tool
from app.tools.files.files import PathInput, path_policy
from app.tools.macos.applescript import NativeRunner
from app.tools.registry import ToolRegistry


def metadata(value: str) -> dict:
    path = checked_path(value)
    try:
        info = path.stat()
    except FileNotFoundError:
        raise ValueError("File or folder no longer exists.") from None
    except PermissionError:
        raise ValueError(
            "macOS denied access. Check Files and Folders permissions for the launching app."
        ) from None
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError("Only ordinary files and folders are supported.")
    result = {
        "path": str(path),
        "name": path.name,
        "kind": "folder" if stat.S_ISDIR(info.st_mode) else "file",
        "modified_at": datetime.fromtimestamp(info.st_mtime).astimezone().isoformat(),
    }
    # A directory's stat size is not the size of its contents.
    if stat.S_ISREG(info.st_mode):
        result["size_bytes"] = info.st_size
        result["extension"] = path.suffix
    if hasattr(info, "st_birthtime"):
        result["created_at"] = datetime.fromtimestamp(info.st_birthtime).astimezone().isoformat()
    return result


class FinderController:
    def __init__(self, runner: NativeRunner):
        self.runner = runner

    async def info(self, args: PathInput) -> dict:
        return await asyncio.to_thread(metadata, args.path)

    async def reveal(self, args: PathInput) -> dict:
        info = await self.info(args)
        # Absolute path is a single argv element, never shell code.
        path = checked_path(info["path"])
        await self.runner.run("/usr/bin/open", "-R", str(path))
        return {"path": str(path), "message": "macOS accepted revealing the item in Finder."}


def register(registry: ToolRegistry, controller: FinderController) -> None:
    registry.register(
        Tool(
            "reveal_in_finder",
            "Select an existing file or folder in Finder without opening it.",
            PathInput,
            RiskLevel.SAFE,
            controller.reveal,
            path_policy,
        )
    )
    registry.register(
        Tool(
            "get_file_info",
            "Inspect a file or folder's metadata after approval. "
            "Returns size for files and timestamps, never contents or recursive folder sizes. "
            "Creation and modification timestamps do not prove download time.",
            PathInput,
            RiskLevel.CONFIRM,
            controller.info,
            path_policy,
        )
    )
