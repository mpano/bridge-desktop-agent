from types import SimpleNamespace

from app.integrations.models import IntegrationError


class SpotifyWebService:
    def __init__(self, accounts):
        self.accounts = accounts

    async def search(self, args):
        data = await self.accounts.request(
            args.account_id,
            "spotify",
            "GET",
            "search",
            params={"q": args.query, "type": args.kind, "limit": args.limit},
        )
        page = data.get(args.kind + "s", {})
        return {
            "items": [
                {
                    "name": item.get("name"),
                    "uri": item.get("uri"),
                    "artists": [a.get("name") for a in item.get("artists", [])],
                    "url": item.get("external_urls", {}).get("spotify"),
                }
                for item in page.get("items", [])
                if item
            ],
            "has_more": bool(page.get("next")),
        }

    async def playlists(self, args):
        data = await self.accounts.request(
            args.account_id,
            "spotify",
            "GET",
            "me/playlists",
            scopes=("playlist-read-private",),
            params={"limit": 50},
        )
        return {
            "playlists": [
                {
                    "name": item.get("name"),
                    "uri": item.get("uri"),
                    "url": item.get("external_urls", {}).get("spotify"),
                }
                for item in data.get("items", [])
                if item
            ],
            "has_more": bool(data.get("next")),
        }

    async def devices(self, args):
        data = await self.accounts.request(
            args.account_id,
            "spotify",
            "GET",
            "me/player/devices",
            scopes=("user-read-playback-state",),
        )
        return {"devices": data.get("devices", [])}

    async def play(self, args):
        body = (
            {"uris": [args.uri]}
            if args.uri.startswith("spotify:track:")
            else {"context_uri": args.uri}
        )
        await self.accounts.request(
            args.account_id,
            "spotify",
            "PUT",
            "me/player/play",
            scopes=("user-modify-playback-state",),
            params={"device_id": args.device_id} if args.device_id else {},
            json=body,
        )
        return {"message": "Spotify accepted the playback request.", "uri": args.uri}

    async def queue(self, args):
        params = {"uri": args.uri}
        if args.device_id:
            params["device_id"] = args.device_id
        await self.accounts.request(
            args.account_id,
            "spotify",
            "POST",
            "me/player/queue",
            scopes=("user-modify-playback-state",),
            params=params,
        )
        return {"message": "Spotify accepted the queue request.", "uri": args.uri}

    async def play_search(self, args, desktop=None):
        """Find the best match and play it, without the model needing the exact URI."""
        found = await self.search(args)
        if not found["items"]:
            raise IntegrationError(f"Spotify found no {args.kind} for “{args.query}”.")
        best = found["items"][0]
        if desktop is not None:
            # The Mac app plays URIs without needing an already-active Connect device.
            await desktop(best["uri"])
        else:
            await self.play(
                SimpleNamespace(account_id=args.account_id, uri=best["uri"], device_id=None)
            )
        return {
            "playing": best["name"],
            "artists": best["artists"],
            "uri": best["uri"],
            "message": "Spotify is playing the top search result.",
        }
