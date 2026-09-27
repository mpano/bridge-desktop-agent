from typing import Literal, Protocol

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class SpotifySearchProvider(Protocol):
    async def search_track(self, query: str) -> list[dict]: ...
    async def play_uri(self, uri: str) -> None: ...


class SpotifyInput(Input):
    action: Literal["open", "activate", "play", "pause", "play_pause", "next", "previous"]


class SpotifyController:
    def __init__(self, runner, script):
        self.runner, self.script = runner, script

    async def control(self, args):
        if args.action == "open":
            await self.runner.run("/usr/bin/open", "-a", "Spotify")
        elif args.action == "activate":
            await self.script.application("Spotify", "activate")
        else:
            command = {
                "play": "play",
                "pause": "pause",
                "play_pause": "playpause",
                "next": "next track",
                "previous": "previous track",
            }[args.action]
            await self.script.run(f'tell application "Spotify" to {command}')
        return {"action": args.action}


def register(registry, controller):
    registry.register(
        Tool(
            "spotify_control",
            "Control Spotify playback; song search not yet supported.",
            SpotifyInput,
            RiskLevel.SAFE,
            controller.control,
        )
    )
