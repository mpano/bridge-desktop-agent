"""Contacts lookup, name → address resolution before approval, and iMessage."""

import base64
import json
from email import message_from_bytes
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.agent.executor import Executor
from app.bootstrap import build_agent
from app.llm.models import LLMResponse, ToolCall
from app.tools.integrations.register import register
from app.tools.integrations.schemas import SendEmailInput
from app.tools.productivity import contacts, messages
from app.tools.productivity.contacts import ContactsDirectory, Person, email_resolver
from app.tools.registry import ToolRegistry
from tests.test_connected_services import manager, settings

PEOPLE = [
    Person("Olivier Mupenzi", ["olivier@example.com"], [("mobile", "+250 788 123 456")]),
    Person("Olivier Kagame", ["ok@example.com"], []),
    Person(
        "Mom",
        ["mom@example.com", "mom@work.example"],
        [("iPhone", "+1 (555) 010-2000"), ("home", "+1 555 010 3000")],
    ),
    Person("Sam Lee", ["sam@example.com"], [("mobile", "+44 7700 900123")], nickname="Sammy"),
]


class FakeBackend:
    def search(self, query):
        wanted = query.casefold()
        return [
            person
            for person in PEOPLE
            if wanted in person.name.casefold() or wanted == person.nickname.casefold()
        ]


def directory():
    return ContactsDirectory(FakeBackend())


async def test_names_become_single_addresses_or_ask_which():
    people = directory()
    assert await people.email_for("Olivier Mupenzi") == "olivier@example.com"
    assert await people.email_for("sam@example.com") == "sam@example.com"
    assert await people.email_for("Sammy") == "sam@example.com"
    with pytest.raises(ValueError, match="Several contacts match “Olivier”"):
        await people.email_for("Olivier")
    with pytest.raises(ValueError, match="several email addresses"):
        await people.email_for("Mom")
    with pytest.raises(ValueError, match="No contact named"):
        await people.email_for("Nobody")


async def test_phone_prefers_the_mobile_number():
    people = directory()
    assert await people.phone_for("Olivier Mupenzi") == "+250 788 123 456"
    assert await people.phone_for("Mom") == "+1 (555) 010-2000"  # The iPhone line.
    assert await people.phone_for("Olivier Kagame") == "ok@example.com"  # Apple ID email.
    assert await people.phone_for("+44 7700 900123") == "+44 7700 900123"


def test_recipients_can_be_names_but_never_header_injection():
    SendEmailInput(to=["Olivier Mupenzi"], subject="Hi", body="Hi")
    for bad in [
        "a@example.com, evil@example.com",
        "Olivier <evil@example.com>",
        "alex@example.com\r\nBcc: evil@example.com",
    ]:
        with pytest.raises(ValidationError):
            SendEmailInput(to=[bad], subject="Hi", body="Hi")


async def test_approval_shows_the_resolved_address_and_sends_to_it():
    sent = []

    def handler(req):
        sent.append(req)
        return httpx.Response(200, json={"id": "sent-1"})

    service, _, _ = manager(handler)
    registry = ToolRegistry()
    register(registry, service, contacts=directory())
    executor = Executor(registry)
    call = ToolCall(
        call_id="c",
        name="email_send",
        arguments={"to": ["Olivier Mupenzi"], "subject": "Hi", "body": "I miss you"},
    )
    pending = await executor.execute(call, "r")
    assert pending["status"] == "confirmation_required"
    assert pending["arguments"]["to"] == ["olivier@example.com"]
    assert not sent
    approved = call.model_copy(update={"arguments": pending["arguments"]})
    done = await executor.execute(approved, "r", approved=True)
    assert done["success"]
    raw = json.loads(sent[0].content)["raw"]
    assert message_from_bytes(base64.urlsafe_b64decode(raw))["To"] == "olivier@example.com"


async def test_ambiguous_name_is_refused_before_approval():
    service, _, _ = manager(lambda req: httpx.Response(200, json={"id": "x"}))
    registry = ToolRegistry()
    register(registry, service, contacts=directory())
    call = ToolCall(
        call_id="c", name="email_send", arguments={"to": ["Olivier"], "subject": "Hi", "body": "Hi"}
    )
    result = await Executor(registry).execute(call, "r")
    assert result["status"] == "failed" and "Say which one" in result["error"]


async def test_agent_approves_and_runs_exactly_the_resolved_arguments(tmp_path):
    sent = []

    def handler(req):
        sent.append(req)
        return httpx.Response(200, json={"id": "sent-1"})

    service, _, _ = manager(handler)
    llm = AsyncMock()
    llm.generate_response.side_effect = [
        LLMResponse(
            calls=[
                ToolCall(
                    call_id="1",
                    name="email_send",
                    arguments={"to": ["Sammy"], "subject": "Hello", "body": "On my way"},
                )
            ]
        ),
        LLMResponse(text="Sent."),
    ]
    agent = build_agent(
        settings(database_path=tmp_path / "db"), llm=llm, runner=AsyncMock(), accounts=service
    )
    agent.executor.registry.get("email_send").resolve = email_resolver(directory(), "to")
    try:
        result = await agent.message("Email Sammy that I'm on my way")
        assert result["confirmation"]["arguments"]["to"] == ["sam@example.com"]
        # Contacts changing after approval can't redirect the message.
        agent.executor.registry.get("email_send").resolve = email_resolver(None, "to")
        done = await agent.confirm(result["confirmation"]["token"], True)
        assert done["status"] == "completed"
        raw = json.loads(sent[0].content)["raw"]
        assert message_from_bytes(base64.urlsafe_b64decode(raw))["To"] == "sam@example.com"
    finally:
        await agent.close()


async def test_imessage_resolves_name_asks_first_and_passes_text_as_argument():
    runner = AsyncMock()
    controller = messages.MessagesController(runner, directory())
    registry = ToolRegistry()
    messages.register(registry, controller)
    executor = Executor(registry)
    call = ToolCall(
        call_id="c",
        name="messages_send",
        arguments={"to": "Mom", "text": 'On my way" & do shell script "x', "to_name": "Boss"},
    )
    pending = await executor.execute(call, "r")
    assert pending["status"] == "confirmation_required"
    assert pending["arguments"]["to"] == "+15550102000"
    assert pending["arguments"]["to_name"] == "Mom"  # Set by Bridge, not the model.
    runner.run.assert_not_awaited()
    approved = call.model_copy(update={"arguments": pending["arguments"]})
    done = await executor.execute(approved, "r", approved=True)
    assert done["success"] and "Mom (+15550102000)" in done["display"]
    argv = runner.run.await_args.args
    assert argv[3:] == ("+15550102000", 'On my way" & do shell script "x', "imessage")
    assert "do shell script" not in argv[2]


def test_contacts_find_renders_people_locally():
    registry = ToolRegistry()
    contacts.register(registry, directory())
    tool = registry.get("contacts_find")
    assert "Olivier Mupenzi — olivier@example.com, +250 788 123 456 (mobile)" in tool.render(
        {
            "query": "olivier",
            "people": [
                {
                    "name": "Olivier Mupenzi",
                    "emails": ["olivier@example.com"],
                    "phones": [("mobile", "+250 788 123 456")],
                }
            ],
        }
    )


async def test_imessage_never_shows_an_unverified_name_for_a_number():
    controller = messages.MessagesController(AsyncMock(), directory())
    lie = messages.MessageInput(to="+44 7700 900123", text="hi", to_name="Mom")
    assert (await controller.resolve(lie)).to_name is None
    truth = messages.MessageInput(to="+44 7700 900123", text="hi", to_name="Sam Lee")
    assert (await controller.resolve(truth)).to_name == "Sam Lee"
