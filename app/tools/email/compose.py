"""Email without an account connection: open a pre-filled compose window; the user sends."""

from typing import Literal
from urllib.parse import quote, urlencode

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.integrations.schemas import Recipient
from app.tools.productivity.contacts import email_resolver

GMAIL = "https://mail.google.com/mail/"


class ComposeInput(Input):
    to: list[Recipient] = Field(
        default_factory=list, max_length=10, description="Email addresses or contact names"
    )
    subject: str = Field(default="", max_length=300, pattern=r"^[^\r\n\x00]*$")
    body: str = Field(default="", max_length=4000)
    client: Literal["gmail", "mail_app"] = Field(
        default="gmail", description="gmail opens Gmail in the browser; mail_app uses Mail.app"
    )


class InboxSearchInput(Input):
    query: str = Field(min_length=1, max_length=300, pattern=r"^[^\x00-\x1f\x7f]+$")


class EmailComposer:
    def __init__(self, runner, open_url, contacts=None):
        self.runner, self.open_url, self.contacts = runner, open_url, contacts

    async def compose(self, args):
        if args.client == "gmail":
            query = {"view": "cm", "fs": "1", "to": ",".join(args.to), "su": args.subject}
            query["body"] = args.body
            await self.open_url(GMAIL + "?" + urlencode(query, quote_via=quote))
        else:
            fields = urlencode({"subject": args.subject, "body": args.body}, quote_via=quote)
            recipients = ",".join(quote(value, safe="@") for value in args.to)
            await self.runner.run("/usr/bin/open", f"mailto:{recipients}?{fields}")
        return {
            "to": args.to,
            "subject": args.subject,
            "message": "Opened a pre-filled draft. Review it and press Send yourself.",
        }

    async def search(self, args):
        await self.open_url(GMAIL + "u/0/#search/" + quote(args.query, safe=""))
        return {"query": args.query, "message": "Opened Gmail search results in the browser."}


def register(registry, composer):
    registry.register(
        Tool(
            "email_compose_in_browser",
            "Open a pre-filled new email in Gmail (browser) or Mail.app for the user to review "
            "and send. Nothing is sent by Bridge. Works without a connected Gmail account.",
            ComposeInput,
            RiskLevel.SAFE,
            composer.compose,
            resolve=email_resolver(composer.contacts, "to"),
        )
    )
    registry.register(
        Tool(
            "email_open_search_in_browser",
            "Open Gmail search results in the browser, e.g. 'from:alex is:unread'. "
            "Works without a connected Gmail account.",
            InboxSearchInput,
            RiskLevel.SAFE,
            composer.search,
        )
    )
