from app.security.permissions import checked_path
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.system.apps import AppInput, app_name


class PathInput(Input):
    path: str


class ProjectInput(PathInput):
    editor: str | None = None


def path_policy(args):
    checked_path(args.path)
    return RiskLevel.SAFE


def register(registry, runner, default_editor):
    async def folder(args):
        path = checked_path(args.path)
        if not path.is_dir():
            raise ValueError("Folder does not exist.")
        await runner.run("/usr/bin/open", str(path))
        return {"path": str(path)}

    async def file(args):
        path = checked_path(args.path)
        if not path.is_file():
            raise ValueError("File does not exist.")
        # Opening files may execute scripts or trigger application macros.
        await runner.run("/usr/bin/open", str(path))
        return {"path": str(path)}

    async def project(args):
        path = checked_path(args.path)
        if not path.is_dir():
            raise ValueError("Project folder does not exist.")
        default = default_editor() if callable(default_editor) else default_editor
        editor = app_name(args.editor or default)
        AppInput(app_name=editor)
        await runner.run("/usr/bin/open", "-a", editor, str(path))
        return {"path": str(path), "editor": editor}

    async def create(args):
        path = checked_path(args.path)
        path.mkdir()
        return {"path": str(path)}

    for name, schema, risk, handler, description in [
        ("open_folder", PathInput, RiskLevel.SAFE, folder, "Open a folder in Finder."),
        ("open_file", PathInput, RiskLevel.CONFIRM, file, "Open a file with its default app."),
        ("open_project", ProjectInput, RiskLevel.SAFE, project, "Open a project in an editor."),
        ("create_folder", PathInput, RiskLevel.CONFIRM, create, "Create one folder."),
    ]:
        registry.register(Tool(name, description, schema, risk, handler, path_policy))
