from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


def register(registry, script):
    async def running(args):
        result = await script.run(
            'tell application "System Events" to get name of every application process '
            "whose background only is false"
        )
        return {"applications": result.split(", ") if result else []}

    registry.register(
        Tool("list_running_apps", "List foreground applications.", Input, RiskLevel.SAFE, running)
    )
