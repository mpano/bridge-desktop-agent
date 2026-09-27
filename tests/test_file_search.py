import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.agent.executor import Executor
from app.llm.models import ToolCall
from app.tools.files.search import FileSearchController, FileSearchInput, register
from app.tools.registry import ToolRegistry


@pytest.fixture
def home(tmp_path):
    for name in ("Downloads", "Desktop", "Documents"):
        (tmp_path / name).mkdir()
    return tmp_path


async def test_filters_and_metadata(home):
    file = home / "Downloads" / "Report.PDF"
    file.write_text("private contents")
    yesterday = datetime.now().replace(hour=12) - timedelta(days=1)
    os.utime(file, (yesterday.timestamp(), yesterday.timestamp()))
    (home / "Downloads" / "other.txt").touch()
    result = await FileSearchController(home).search(
        FileSearchInput(name_contains="report", extension=".PDF", modified="yesterday")
    )
    assert [item["name"] for item in result["files"]] == ["Report.PDF"]
    assert result["files"][0]["size_bytes"] == 16
    assert "private contents" not in str(result)
    assert not result["truncated"]


async def test_recursive_excludes_hidden_protected_and_links(home):
    root = home / "Downloads"
    for name in ("ordinary", ".ssh", "Keychains", "Google", ".env.backup"):
        folder = root / name
        folder.mkdir()
        (folder / "secret.pdf").touch()
    outside = home / "outside"
    outside.mkdir()
    (outside / "outside.pdf").touch()
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "file-link.pdf").symlink_to(outside / "outside.pdf")
    controller = FileSearchController(home)
    assert (await controller.search(FileSearchInput()))["files"] == []
    result = await controller.search(FileSearchInput(recursive=True))
    assert [item["path"] for item in result["files"]] == [str(root / "ordinary/secret.pdf")]


async def test_scope_symlink_rejected(home):
    (home / "Downloads").rmdir()
    (home / "Downloads").symlink_to(home / "Documents", target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        await FileSearchController(home).search(FileSearchInput())


async def test_budgets_report_partial_results(home):
    for index in range(5):
        (home / "Downloads" / f"{index}.txt").touch()
    result = await FileSearchController(home, max_entries=2).search(FileSearchInput())
    assert result["truncated"]
    assert result["examined_entries"] == 2
    result = await FileSearchController(home).search(FileSearchInput(limit=1))
    assert result["truncated"]
    assert len(result["files"]) == 1


@pytest.mark.parametrize(
    "values",
    [
        {"scope": "/"},
        {"extension": "../env"},
        {"limit": 101},
        {"recursive": "true"},
    ],
)
def test_invalid_arguments(values):
    with pytest.raises(ValidationError):
        FileSearchInput(**values)


async def test_search_requires_approval_and_does_not_persist_arguments():
    controller = AsyncMock()
    controller.search.return_value = {"files": []}
    registry = ToolRegistry()
    register(registry, controller)
    executor = Executor(registry)
    call = ToolCall(call_id="search", name="find_files", arguments={"extension": "pdf"})
    assert (await executor.execute(call, "request"))["status"] == "confirmation_required"
    controller.search.assert_not_called()
    assert (await executor.execute(call, "request", approved=True))["success"]
    controller.search.assert_awaited_once()
    assert not registry.get("find_files").persist_arguments


async def test_permission_failure_is_helpful(home, monkeypatch):
    def denied(_):
        raise PermissionError("denied")

    monkeypatch.setattr("app.tools.files.search.os.scandir", denied)
    with pytest.raises(ValueError, match="Files and Folders"):
        await FileSearchController(home).search(FileSearchInput())
