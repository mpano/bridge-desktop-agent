from pydantic import Field, field_validator

from app.memory.models import Preference
from app.memory.store import MemoryRepository
from app.security.permissions import checked_path
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.registry import ToolRegistry


class ProjectAlias(Input):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[\w .-]+$")

    @field_validator("name")
    @classmethod
    def normalize(cls, value: str) -> str:
        value = " ".join(value.casefold().split())
        if not value:
            raise ValueError("Project name cannot be blank.")
        return value


class RememberProject(ProjectAlias):
    path: str = Field(min_length=1, max_length=4096)


def register(registry: ToolRegistry, memory: MemoryRepository) -> None:
    def validate_path(args):
        checked_path(args.path)
        return RiskLevel.CONFIRM

    async def remember(args):
        validate_path(args)
        path = str(checked_path(args.path))
        if not checked_path(path).is_dir():
            raise ValueError("Project directory does not exist.")
        memory.set(Preference("project:" + args.name, path))
        return {"name": args.name, "path": path, "message": "Project alias saved."}

    async def lookup(args):
        preference = memory.get("project:" + args.name)
        if preference is None:
            raise ValueError("Unknown project alias. Ask the user for its directory.")
        path = checked_path(preference.value)
        if not path.is_dir():
            raise ValueError("Saved project directory no longer exists. Update its alias.")
        return {"name": args.name, "path": str(path)}

    async def listing(args):
        return {
            "projects": [
                {"name": p.key.removeprefix("project:"), "path": p.value} for p in memory.projects()
            ]
        }

    async def forget(args):
        if not memory.delete_project(args.name):
            raise ValueError("Unknown project alias.")
        return {"name": args.name, "message": "Alias removed; project files were not changed."}

    registry.register(
        Tool(
            "remember_project",
            "Save or replace a project alias for an existing directory. Requires confirmation.",
            RememberProject,
            RiskLevel.CONFIRM,
            remember,
            validate_path,
        )
    )
    registry.register(
        Tool(
            "lookup_project",
            "Resolve a saved project alias to an existing path.",
            ProjectAlias,
            RiskLevel.SAFE,
            lookup,
        )
    )
    registry.register(
        Tool("list_projects", "List saved project aliases.", Input, RiskLevel.SAFE, listing)
    )
    registry.register(
        Tool(
            "forget_project",
            "Remove an alias without deleting project files.",
            ProjectAlias,
            RiskLevel.CONFIRM,
            forget,
        )
    )
