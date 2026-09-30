"""Send iMessage or SMS through the Messages app. Always asks for approval first.

Recipients can be contact names; they're resolved to a number or Apple ID before the
approval, which shows who the message goes to. Bridge can't confirm delivery.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.productivity.contacts import EMAIL, ContactsDirectory
from app.tools.productivity.reminders import run_script

SEND_SCRIPT = """
on run argv
    set {target, body, kind} to argv
    tell application "Messages"
        if kind is "sms" then
            set chosen to 1st account whose service type = SMS
        else
            set chosen to 1st account whose service type = iMessage
        end if
        send body to participant target of chosen
    end tell
end run
"""


class MessageInput(Input):
    to: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[^\x00-\x1f\x7f]+$",
        description="A contact's name, a phone number, or an Apple ID email",
    )
    text: str = Field(min_length=1, max_length=4000)
    service: Literal["imessage", "sms"] = Field(
        default="imessage", description="sms needs Text Message Forwarding from an iPhone"
    )
    to_name: str | None = Field(default=None, description="Filled in by Bridge from Contacts")


def normalize(handle: str) -> str:
    if EMAIL.fullmatch(handle):
        return handle
    digits = re.sub(r"[^0-9+]", "", handle)
    if not re.fullmatch(r"\+?[0-9]{6,16}", digits):
        raise ValueError(f"“{handle}” isn't a phone number or Apple ID email.")
    return digits


class MessagesController:
    def __init__(self, runner, contacts: ContactsDirectory | None = None):
        self.runner, self.contacts = runner, contacts

    async def _verified_name(self, name: str | None, handle: str) -> str | None:
        """Keep a display name only if Contacts says it belongs to exactly this handle."""
        if not name or self.contacts is None:
            return None
        try:
            return name if normalize(await self.contacts.phone_for(name)) == handle else None
        except ValueError:
            return None

    async def resolve(self, args):
        value = args.to.strip()
        if EMAIL.fullmatch(value) or re.fullmatch(r"\+?[0-9 ().-]{6,20}", value):
            handle = normalize(value)
            name = await self._verified_name(args.to_name, handle)
        else:
            if self.contacts is None:
                raise ValueError(f"“{value}” isn't a phone number. Give the number.")
            handle = normalize(await self.contacts.phone_for(value))
            name = value
        # The approval shows a name only when it comes from Contacts, never on trust.
        return args.model_copy(update={"to": handle, "to_name": name})

    async def send(self, args):
        await run_script(self.runner, "Messages", SEND_SCRIPT, args.to, args.text, args.service)
        who = f"{args.to_name} ({args.to})" if args.to_name else args.to
        via = "SMS" if args.service == "sms" else "iMessage"
        return {
            "to": args.to,
            "to_name": args.to_name,
            "service": args.service,
            "message": f"Messages accepted the {via} to {who}. Delivery isn't confirmed.",
        }


def register(registry, controller: MessagesController):
    registry.register(
        Tool(
            "messages_send",
            "Send an iMessage (or SMS) with the Messages app, e.g. 'text Mom I'm on my way'. "
            "Give the contact's name; Bridge finds their number. Asks for approval first.",
            MessageInput,
            RiskLevel.CONFIRM,
            controller.send,
            confirmation_message="Send this message? Check the recipient and the text.",
            render=lambda data: "✓ " + data["message"],
            resolve=controller.resolve,
        )
    )
