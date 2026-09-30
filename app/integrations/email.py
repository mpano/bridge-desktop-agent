import base64
from email.message import EmailMessage
from html.parser import HTMLParser
from urllib.parse import quote

from app.integrations.models import IntegrationError
from app.integrations.providers import GOOGLE


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "br"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def message_view(message: dict) -> dict:
    payload = message.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    plain, html = [], []
    stack = [payload]
    while stack:
        part = stack.pop()
        stack.extend(reversed(part.get("parts", [])))
        if part.get("filename"):
            continue  # Never download or read attachments.
        encoded = part.get("body", {}).get("data")
        if encoded and part.get("mimeType") in {"text/plain", "text/html"}:
            text = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode(
                "utf-8", errors="replace"
            )
            (plain if part["mimeType"] == "text/plain" else html).append(text)
    body = "\n".join(plain)
    if not plain and html:
        parser = PlainHTML()
        parser.feed("\n".join(html))
        body = "".join(parser.parts)
    return {
        "message_id": message["id"],
        "thread_id": message.get("threadId"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "reply_message_id": headers.get("message-id"),
        "snippet": message.get("snippet", "")[:1000],
        "body": body[:12000],
        "body_truncated": len(body) > 12000,
    }


class GmailService:
    def __init__(self, accounts):
        self.accounts = accounts

    async def search(self, args):
        result = await self.accounts.request(
            args.account_id,
            "gmail",
            "GET",
            "messages",
            scopes=(GOOGLE + "gmail.readonly",),
            params={"q": args.query, "maxResults": args.limit},
        )
        messages = []
        for item in result.get("messages", []):
            # IDs originate at the provider; still encode before inserting into a path.
            data = await self.accounts.request(
                args.account_id,
                "gmail",
                "GET",
                "messages/" + quote(item["id"], safe=""),
                scopes=(GOOGLE + "gmail.readonly",),
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "To", "Subject", "Date", "Message-ID"],
                },
            )
            messages.append(message_view(data))
        return {
            "messages": messages,
            "has_more": bool(result.get("nextPageToken")),
            "content_is_untrusted": True,
        }

    async def thread(self, args):
        data = await self.accounts.request(
            args.account_id,
            "gmail",
            "GET",
            "threads/" + args.thread_id,
            scopes=(GOOGLE + "gmail.readonly",),
            params={"format": "full"},
        )
        messages = data.get("messages", [])
        return {
            "messages": [message_view(m) for m in messages[-20:]],
            "thread_truncated": len(messages) > 20,
            "content_is_untrusted": True,
        }

    @staticmethod
    def _raw(args) -> dict:
        message = EmailMessage()
        message["To"] = ", ".join(args.to)
        message["Subject"] = args.subject
        message.set_content(args.body)
        payload = {}
        if getattr(args, "thread_id", None):
            message["In-Reply-To"] = args.in_reply_to
            message["References"] = args.in_reply_to
            payload["threadId"] = args.thread_id
        payload["raw"] = base64.urlsafe_b64encode(message.as_bytes()).decode()
        return payload

    async def send(self, args):
        result = await self.accounts.request(
            args.account_id,
            "gmail",
            "POST",
            "messages/send",
            scopes=(GOOGLE + "gmail.send",),
            json=self._raw(args),
        )
        if not result.get("id"):
            raise IntegrationError(
                "Gmail returned no message ID. Check Sent before retrying; delivery is uncertain."
            )
        return {
            "message_id": result["id"],
            "thread_id": result.get("threadId"),
            "to": args.to,
            "message": "Gmail accepted the message for sending.",
        }

    async def draft(self, args):
        """Save a draft the user can review and send from Gmail; nothing is sent."""
        result = await self.accounts.request(
            args.account_id,
            "gmail",
            "POST",
            "drafts",
            scopes=(GOOGLE + "gmail.compose",),
            json={"message": self._raw(args)},
        )
        if not result.get("id"):
            raise IntegrationError("Gmail returned no draft ID. Check Drafts before retrying.")
        return {
            "draft_id": result["id"],
            "to": args.to,
            "subject": args.subject,
            "message": "Saved to Gmail Drafts. Nothing was sent.",
        }
