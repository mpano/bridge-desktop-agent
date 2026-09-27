from pathlib import Path

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class AppSearch(Input):
    query: str = Field(default="", max_length=100)


class ApplicationCatalog:
    """Shallow enumeration of standard app directories; does not launch applications."""

    def __init__(self, roots: list[Path] | None = None):
        self.roots = (
            roots
            if roots is not None
            else [
                Path("/Applications"),
                Path("/System/Applications"),
                Path.home() / "Applications",
            ]
        )

    def search(self, query: str) -> dict:
        found: dict[str, dict] = {}
        unreadable = []

        def visit(directory: Path, depth: int) -> None:
            try:
                children = list(directory.iterdir())
            except OSError:
                unreadable.append(str(directory))
                return
            for child in children:
                if child.is_symlink() or not child.is_dir():
                    continue
                if child.suffix.casefold() == ".app":
                    if query.casefold() in child.stem.casefold():
                        found[str(child)] = {"name": child.stem, "path": str(child)}
                elif depth < 2 and not child.name.startswith("."):
                    visit(child, depth + 1)

        for root in self.roots:
            if root.exists():
                visit(root, 0)
        matches = sorted(found.values(), key=lambda item: (item["name"].casefold(), item["path"]))
        return {
            "applications": matches[:100],
            "truncated": len(matches) > 100,
            "unreadable_directories": unreadable,
            "scope": "Standard application directories, up to two subfolder levels.",
        }


def register(registry, catalog: ApplicationCatalog):
    async def search(args):
        return catalog.search(args.query)

    registry.register(
        Tool(
            "list_installed_apps",
            "Find installed application names by optional "
            "substring. Use to resolve ambiguous app or IDE requests; results "
            "describe installed apps, not their language capabilities.",
            AppSearch,
            RiskLevel.SAFE,
            search,
        )
    )
