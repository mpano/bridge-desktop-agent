import re
from uuid import uuid4

from app.integrations.models import IntegrationError

CONVERSATION_SCOPES = ("channels:read", "groups:read", "im:read", "mpim:read")
HISTORY_SCOPES = ("channels:history", "groups:history", "im:history", "mpim:history")
SLACK_ID = re.compile(r"[CGD][A-Z0-9]{8,32}")
PAGE_LIMIT = 5  # At most five provider pages per lookup; ask for an exact ID beyond that.


class SlackService:
    def __init__(self, accounts):
        self.accounts = accounts

    async def _paged(self, account_id, method, key, scopes, params):
        items, cursor = [], ""
        for _ in range(PAGE_LIMIT):
            data = await self.accounts.request(
                account_id,
                "slack",
                "GET",
                method,
                scopes=scopes,
                params={**params, "limit": 200, **({"cursor": cursor} if cursor else {})},
            )
            items += data.get(key, [])
            cursor = data.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
        return items

    async def _conversations(self, account_id):
        return await self._paged(
            account_id,
            "conversations.list",
            "channels",
            CONVERSATION_SCOPES,
            {"types": "public_channel,private_channel,im,mpim", "exclude_archived": "true"},
        )

    async def _users(self, account_id):
        users = await self._paged(account_id, "users.list", "members", ("users:read",), {})
        return [user for user in users if not user.get("deleted")]

    @staticmethod
    def _user_names(user: dict) -> set[str]:
        profile = user.get("profile", {})
        names = {user.get("name"), user.get("real_name"), profile.get("display_name")}
        names |= {profile.get("real_name"), profile.get("email")}
        return {value.casefold() for value in names if value}

    async def resolve(self, account_id, destination: str) -> tuple[str, str]:
        """Map "#general", "general", "@alex" or "Alex Kim" to one exact conversation.

        Ambiguous or unknown names fail; Bridge never picks a destination by guesswork.
        """
        text = destination.strip()
        if SLACK_ID.fullmatch(text):
            return text, text
        wanted = text.lstrip("#@").casefold()
        if not text.startswith("@"):
            matches = [
                item
                for item in await self._conversations(account_id)
                if (item.get("name") or "").casefold() == wanted
            ]
            if len(matches) == 1:
                return matches[0]["id"], "#" + matches[0]["name"]
        people = [
            user for user in await self._users(account_id) if wanted in self._user_names(user)
        ]
        if len(people) > 1:
            names = ", ".join(sorted(user.get("real_name") or user["name"] for user in people))
            raise IntegrationError(f"Several Slack people match “{text}”: {names}. Be specific.")
        if not people:
            raise IntegrationError(f"No Slack channel or person named “{text}” was found.")
        person = people[0]
        opened = await self.accounts.request(
            account_id,
            "slack",
            "POST",
            "conversations.open",
            scopes=("im:write",),
            json={"users": person["id"]},
        )
        channel = opened.get("channel", {}).get("id")
        if not channel:
            raise IntegrationError("Slack did not open a direct message with that person.")
        return channel, "@" + (person.get("real_name") or person["name"])

    async def search(self, args):
        data = await self.accounts.request(
            args.account_id,
            "slack",
            "GET",
            "search.messages",
            scopes=("search:read",),
            params={"query": args.query, "count": args.limit, "highlight": "false"},
        )
        messages = data.get("messages", {})
        return {
            "messages": [
                {
                    "text": item.get("text"),
                    "user": item.get("username") or item.get("user"),
                    "ts": item.get("ts"),
                    "channel": (item.get("channel") or {}).get("name"),
                    "permalink": item.get("permalink"),
                }
                for item in messages.get("matches", [])
            ],
            "total": messages.get("total"),
            "content_is_untrusted": True,
        }

    async def channels(self, args):
        items = await self._conversations(args.account_id)
        return {
            "channels": [
                {key: item.get(key) for key in ("id", "name", "user", "is_private", "is_member")}
                for item in items
            ],
        }

    async def history(self, args):
        channel, label = await self.resolve(args.account_id, args.channel)
        data = await self.accounts.request(
            args.account_id,
            "slack",
            "GET",
            "conversations.history",
            scopes=HISTORY_SCOPES,
            params={"channel": channel, "limit": args.limit},
        )
        names = {}
        try:
            names = {
                user["id"]: user.get("real_name") or user.get("name")
                for user in await self._users(args.account_id)
            }
        except IntegrationError:
            pass  # Names are a convenience; fall back to user IDs.
        return {
            "channel": label,
            "messages": [
                {
                    "user": names.get(item.get("user"), item.get("user") or item.get("username")),
                    "text": item.get("text"),
                    "ts": item.get("ts"),
                }
                for item in reversed(data.get("messages", []))
            ],
            "has_more": bool(data.get("has_more")),
            "content_is_untrusted": True,
        }

    async def send(self, args):
        channel, label = await self.resolve(args.account_id, args.channel)
        payload = {
            "channel": channel,
            "text": args.text,
            "mrkdwn": False,
            "parse": "none",
            "unfurl_links": False,
            "unfurl_media": False,
            "client_msg_id": str(uuid4()),
        }
        if args.thread_ts:
            payload["thread_ts"] = args.thread_ts
        data = await self.accounts.request(
            args.account_id,
            "slack",
            "POST",
            "chat.postMessage",
            scopes=("chat:write",),
            json=payload,
        )
        if not data.get("ts") or not data.get("channel"):
            raise IntegrationError(
                "Slack returned no message receipt. Check the channel before retrying."
            )
        return {
            "channel_id": data["channel"],
            "destination": label,
            "message_ts": data["ts"],
            "message": f"Slack accepted the message for {label}.",
        }
