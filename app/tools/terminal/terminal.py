import re
import shlex
from pathlib import Path

from app.security.permissions import PolicyError, checked_path
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class TerminalInput(Input):
    command: str
    cwd: str | None = None


class CommandPolicy:
    SAFE = {
        ("pwd",),
        ("ls",),
        ("ls", "-l"),
        ("ls", "-la"),
        ("git", "status"),
        ("git", "branch"),
        ("git", "log"),
        ("whoami",),
        ("date",),
    }
    # Deliberately finite extensions: unknown executables cannot be made safe by confirmation.
    CONFIRM = {("git", "diff"), ("git", "diff", "--stat"), ("git", "show")}
    BLOCKED = {
        "rm",
        "sudo",
        "chmod",
        "chown",
        "kill",
        "pkill",
        "shutdown",
        "reboot",
        "diskutil",
        "dd",
        "sh",
        "bash",
        "zsh",
        "python",
        "python3",
        "osascript",
        "curl",
        "wget",
        "env",
        "open",
        "exec",
        "eval",
    }

    def parse(self, args: TerminalInput) -> tuple[list[str], RiskLevel]:
        checked_path(args.cwd or str(Path.cwd()))
        if re.search(r"[;&|<>$`\n\r\x00]", args.command):
            raise PolicyError("Shell syntax and command composition are blocked.")
        try:
            argv = shlex.split(args.command)
        except ValueError as exc:
            raise PolicyError("Malformed command.") from exc
        if not argv or argv[0] in self.BLOCKED or "/" in argv[0]:
            raise PolicyError("Executable is blocked.")
        command = tuple(argv)
        if command in self.SAFE:
            return argv, RiskLevel.SAFE
        if command in self.CONFIRM:
            return argv, RiskLevel.CONFIRM
        raise PolicyError("Unsupported command or flags. Add a reviewed command policy first.")

    def classify(self, args):
        return self.parse(args)[1]


def register(registry, runner):
    policy = CommandPolicy()

    async def execute(args):
        argv, _ = policy.parse(args)
        cwd = checked_path(args.cwd or str(Path.cwd()))
        if not cwd.is_dir():
            raise ValueError("Working directory does not exist.")
        executable = (
            "/bin/" + argv[0] if argv[0] in {"pwd", "ls", "date"} else "/usr/bin/" + argv[0]
        )
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(Path.home()),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
        }
        command = [executable, *argv[1:]]
        if argv[0] == "git":
            command = [
                executable,
                "--no-pager",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                *argv[1:],
            ]
            if argv[1] in {"log", "show"}:
                command += ["--no-ext-diff", "--no-textconv", "-n", "20"]
            if argv[1] == "diff":
                command += ["--no-ext-diff", "--no-textconv"]
        output = await runner.run(*command, cwd=str(cwd), env=env)
        return {"output": output, "cwd": str(cwd)}

    registry.register(
        Tool(
            "run_terminal_command",
            "Run a reviewed read-only command, without a shell or Terminal window. "
            "Safe: pwd, ls [-l|-la], git status/branch/log, whoami, date. "
            "Confirmation: git diff, git diff --stat, git show.",
            TerminalInput,
            RiskLevel.SAFE,
            execute,
            policy.classify,
        )
    )
