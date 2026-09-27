from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class VolumeInput(Input):
    level: int = Field(ge=0, le=100)


def register(registry, script):
    async def get(args):
        return {"level": int(await script.run("output volume of (get volume settings)"))}

    async def set_level(args):
        await script.run(f"set volume output volume {args.level}")
        return await get(args)

    registry.register(Tool("get_volume", "Get output volume.", Input, RiskLevel.SAFE, get))
    registry.register(
        Tool(
            "set_volume", "Set output volume from 0 to 100.", VolumeInput, RiskLevel.SAFE, set_level
        )
    )
