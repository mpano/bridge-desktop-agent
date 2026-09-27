from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.macos.applescript import MacOSAppleScript, NativeRunner


class AppInput(Input):
    app_name: str = Field(min_length=1, max_length=150, pattern=r"^[\w .+()-]+$")


ALIASES = {
    "chrome": "Google Chrome",
    "vs code": "Visual Studio Code",
    "vscode": "Visual Studio Code",
}


def app_name(value: str) -> str:
    return ALIASES.get(value.lower(), value)


def register(registry, runner: NativeRunner, script: MacOSAppleScript):
    async def launch(args):
        name = app_name(args.app_name)
        await runner.run("/usr/bin/open", "-a", name)
        return {"message": f"macOS accepted opening {name}."}

    registry.register(
        Tool("open_app", "Open an installed application by name.", AppInput, RiskLevel.SAFE, launch)
    )
    for tool_name, action, risk in [
        ("activate_app", "activate", RiskLevel.SAFE),
        ("close_app", "quit", RiskLevel.CONFIRM),
        ("is_app_running", "running", RiskLevel.SAFE),
    ]:

        async def handle(args, action=action):
            result = await script.application(app_name(args.app_name), action)
            return {"action": action, "application": args.app_name, "result": result}

        registry.register(Tool(tool_name, f"{action} an application.", AppInput, risk, handle))
