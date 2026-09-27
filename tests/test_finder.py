from unittest.mock import AsyncMock

import pytest

from app.agent.executor import Executor
from app.llm.models import ToolCall
from app.tools.files.finder import FinderController, register
from app.tools.registry import ToolRegistry


def setup():
    runner = AsyncMock()
    registry = ToolRegistry()
    register(registry, FinderController(runner))
    return Executor(registry), runner


async def test_reveal_uses_one_path_argument(tmp_path):
    path = tmp_path / "report $(whoami); final.pdf"
    path.touch()
    executor, runner = setup()
    result = await executor.execute(
        ToolCall(call_id="1", name="reveal_in_finder", arguments={"path": str(path)}), "test"
    )
    assert result["success"]
    runner.run.assert_awaited_once_with("/usr/bin/open", "-R", str(path))


async def test_metadata_approval_and_folder_size(tmp_path):
    executor, runner = setup()
    path = tmp_path / "test.txt"
    path.write_text("private contents")
    call = ToolCall(call_id="1", name="get_file_info", arguments={"path": str(path)})
    assert (await executor.execute(call, "test"))["status"] == "confirmation_required"
    result = await executor.execute(call, "test", approved=True)
    assert result["result"]["size_bytes"] == 16
    assert "private contents" not in str(result)
    call.arguments["path"] = str(tmp_path)
    result = await executor.execute(call, "test", approved=True)
    assert result["result"]["kind"] == "folder"
    assert "size_bytes" not in result["result"]
    runner.run.assert_not_called()


@pytest.mark.parametrize("name", ["reveal_in_finder", "get_file_info"])
async def test_protected_target_and_symlink_blocked(tmp_path, name):
    protected = tmp_path / ".env"
    protected.touch()
    link = tmp_path / "alias"
    link.symlink_to(protected)
    executor, runner = setup()
    for path in (protected, link):
        result = await executor.execute(
            ToolCall(call_id="1", name=name, arguments={"path": str(path)}),
            "test",
            approved=True,
        )
        assert not result["success"]
    runner.run.assert_not_called()


async def test_missing_and_native_failure_do_not_report_success(tmp_path):
    executor, runner = setup()
    path = tmp_path / "absent"
    call = ToolCall(call_id="1", name="reveal_in_finder", arguments={"path": str(path)})
    assert not (await executor.execute(call, "test"))["success"]
    runner.run.assert_not_called()
    path.touch()
    runner.run.side_effect = RuntimeError("macOS operation failed")
    assert not (await executor.execute(call, "test"))["success"]
