from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.executor import Executor
from app.llm.models import ToolCall
from app.tools.macos.applescript import NativeRunner
from app.tools.registry import ToolRegistry
from app.tools.system.clipboard import ClipboardController, register


@pytest.mark.parametrize(
    "name,args",
    [
        ("read_clipboard", {}),
        ("copy_to_clipboard", {"text": "hello"}),
    ],
)
async def test_clipboard_requires_confirmation(name, args):
    runner = AsyncMock()
    registry = ToolRegistry()
    register(registry, ClipboardController(runner))
    result = await Executor(registry).execute(
        ToolCall(call_id="1", name=name, arguments=args), "test"
    )
    assert result["status"] == "confirmation_required"
    runner.run.assert_not_called()
    assert not registry.get(name).persist_arguments
    assert registry.get(name).confirmation_message


async def test_read_preserves_whitespace_and_limits_output():
    runner = AsyncMock()
    controller = ClipboardController(runner)
    runner.run.return_value = "  hello 🌍\n"
    assert (await controller.read(None))["text"] == "  hello 🌍\n"
    assert runner.run.call_args.kwargs["strip_output"] is False
    runner.run.return_value = "x" * 8001
    with pytest.raises(ValueError, match="no text was returned"):
        await controller.read(None)


async def test_copy_uses_stdin_and_native_failure_propagates():
    runner = AsyncMock()
    registry = ToolRegistry()
    register(registry, ClipboardController(runner))
    call = ToolCall(call_id="1", name="copy_to_clipboard", arguments={"text": "$(whoami)\n"})
    result = await Executor(registry).execute(call, "test", approved=True)
    assert result["success"]
    assert runner.run.call_args.args == ("/usr/bin/pbcopy",)
    assert runner.run.call_args.kwargs["input_text"] == "$(whoami)\n"
    runner.run.side_effect = RuntimeError("Native failure")
    assert not (await Executor(registry).execute(call, "test", approved=True))["success"]


async def test_native_runner_writes_utf8_and_preserves_output(monkeypatch):
    process = Mock()
    process.stdout.read = AsyncMock(side_effect=[b"  output\n", b""])
    process.stderr.read = AsyncMock(return_value=b"")
    process.stdin.drain = AsyncMock()
    process.wait = AsyncMock()
    process.returncode = 0
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("app.tools.macos.applescript.sys.platform", "darwin")
    monkeypatch.setattr("app.tools.macos.applescript.asyncio.create_subprocess_exec", spawn)
    result = await NativeRunner().run("/mock", input_text="🌍\n", strip_output=False)
    process.stdin.write.assert_called_once_with("🌍\n".encode())
    process.stdin.close.assert_called_once()
    assert result == "  output\n"
    with pytest.raises(ValueError, match="input exceeded"):
        await NativeRunner().run("/mock", input_text="x" * 65537)
    spawn.assert_awaited_once()
