from typing import Literal, Protocol
from urllib.parse import quote

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class SpotifySearchProvider(Protocol):
    async def search_track(self, query: str) -> list[dict]: ...
    async def play_uri(self, uri: str) -> None: ...


class SpotifyInput(Input):
    action: Literal[
        "open",
        "activate",
        "play",
        "pause",
        "play_pause",
        "next",
        "previous",
        "now_playing",
        "shuffle_on",
        "shuffle_off",
        "repeat_on",
        "repeat_off",
    ]


class SpotifyVolumeInput(Input):
    level: int = Field(ge=0, le=100, description="Spotify app volume, 0–100")


class SpotifyURIInput(Input):
    uri: str = Field(pattern=r"^spotify:(track|playlist|album|artist):[A-Za-z0-9]{22}$")


class SpotifyOpenSearchInput(Input):
    query: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")


NOW_PLAYING = """
if application "Spotify" is not running then return "not_running"
tell application "Spotify"
    if player state is stopped then return "stopped"
    set t to current track
    return (player state as text) & linefeed & (name of t) & linefeed & (artist of t) & linefeed ¬
        & (album of t) & linefeed & (spotify url of t) & linefeed & (sound volume as text)
end tell
"""


def render_now_playing(data: dict) -> str:
    if data.get("state") == "not_running":
        return "Spotify isn't open."
    if data.get("state") == "stopped":
        return "Spotify is not playing anything."
    verb = "Playing" if data.get("state") == "playing" else "Paused"
    return f"♫ {verb}: {data['track']} — {data['artist']} ({data['album']})"


class SpotifyController:
    """Controls the Spotify Mac app with AppleScript; no account connection needed."""

    def __init__(self, runner, script):
        self.runner, self.script = runner, script

    async def _tell(self, body: str, *values: str) -> str:
        # Values travel as osascript arguments, never spliced into script source.
        return await self.runner.run(
            "/usr/bin/osascript",
            "-e",
            f'on run argv\ntell application "Spotify"\n{body}\nend tell\nend run',
            *values,
        )

    async def now_playing(self) -> dict:
        output = await self.script.run(NOW_PLAYING)
        if output.strip() in {"stopped", "not_running"}:
            return {"state": output.strip()}
        state, track, artist, album, uri, volume = (output.split("\n") + [""] * 6)[:6]
        return {
            "state": state,
            "track": track,
            "artist": artist,
            "album": album,
            "uri": uri,
            "volume": volume,
        }

    async def control(self, args):
        if args.action == "open":
            await self.runner.run("/usr/bin/open", "-a", "Spotify")
        elif args.action == "activate":
            await self.script.application("Spotify", "activate")
        elif args.action == "now_playing":
            return {"action": args.action, **await self.now_playing()}
        else:
            command = {
                "play": "play",
                "pause": "pause",
                "play_pause": "playpause",
                "next": "next track",
                "previous": "previous track",
                "shuffle_on": "set shuffling to true",
                "shuffle_off": "set shuffling to false",
                "repeat_on": "set repeating to true",
                "repeat_off": "set repeating to false",
            }[args.action]
            await self.script.run(f'tell application "Spotify" to {command}')
        return {"action": args.action}

    async def set_volume(self, args):
        await self._tell("set sound volume to (item 1 of argv as integer)", str(args.level))
        return {"volume": args.level}

    async def play_uri(self, uri: str) -> dict:
        await self._tell("play track (item 1 of argv)", uri)
        return {"uri": uri, "message": "Spotify is playing."}

    async def open_search(self, args):
        await self.runner.run("/usr/bin/open", "spotify:search:" + quote(args.query, safe=""))
        return {"query": args.query, "message": "Opened Spotify search results."}


def register(registry, controller):
    registry.register(
        Tool(
            "spotify_control",
            "Control the Spotify Mac app: open, play, pause, next, previous, now_playing, "
            "shuffle and repeat. Works without a connected Spotify account.",
            SpotifyInput,
            RiskLevel.SAFE,
            controller.control,
            render=lambda data: (
                render_now_playing(data) if data.get("action") == "now_playing" else None
            ),
        )
    )
    registry.register(
        Tool(
            "spotify_set_volume",
            "Set the Spotify Mac app's volume from 0 to 100.",
            SpotifyVolumeInput,
            RiskLevel.SAFE,
            controller.set_volume,
        )
    )
    registry.register(
        Tool(
            "spotify_play_uri",
            "Play an exact spotify: track/album/playlist/artist URI in the Spotify Mac app.",
            SpotifyURIInput,
            RiskLevel.SAFE,
            lambda args: controller.play_uri(args.uri),
        )
    )
    registry.register(
        Tool(
            "spotify_open_search",
            "Show Spotify search results for a song, artist or playlist in the Mac app. Use when "
            "no Spotify account is connected; with one, prefer spotify_play_search.",
            SpotifyOpenSearchInput,
            RiskLevel.SAFE,
            controller.open_search,
        )
    )
