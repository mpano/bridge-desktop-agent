"""The Mac's Contacts, for finding people and turning names into addresses.

Send tools resolve names ("Olivier") here *before* asking for approval, so the approval
shows the real address and that exact address is used. Ambiguous names are refused.
"""

from __future__ import annotations

import asyncio
import re
import threading
from dataclasses import dataclass, field

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool

EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE = re.compile(r"^\+?[0-9 ().-]{6,20}$")
DENIED = (
    "Bridge can't read your contacts. Allow it in System Settings > Privacy & Security > "
    "Contacts, then try again."
)


@dataclass(frozen=True)
class Person:
    name: str
    emails: list[str] = field(default_factory=list)
    phones: list[tuple[str, str]] = field(default_factory=list)  # (label, number)
    nickname: str = ""
    organization: str = ""


class ContactsBackend:
    """Blocking Contacts.framework calls, run off the event loop."""

    def __init__(self):
        self._guard = threading.Lock()
        self._store = None

    def _ready(self):
        import Contacts as CN

        status = CN.CNContactStore.authorizationStatusForEntityType_(CN.CNEntityTypeContacts)
        if self._store is None:
            self._store = CN.CNContactStore.alloc().init()
        if status == CN.CNAuthorizationStatusNotDetermined:
            answered, outcome = threading.Event(), {}

            def handler(granted, _error):
                outcome["granted"] = bool(granted)
                answered.set()

            self._store.requestAccessForEntityType_completionHandler_(
                CN.CNEntityTypeContacts, handler
            )
            if not answered.wait(120) or not outcome.get("granted"):
                raise ValueError(DENIED)
        elif status not in {
            CN.CNAuthorizationStatusAuthorized,
            getattr(CN, "CNAuthorizationStatusLimited", 4),
        }:
            raise ValueError(DENIED)
        return self._store

    def search(self, query: str) -> list[Person]:
        import Contacts as CN

        keys = [
            CN.CNContactGivenNameKey,
            CN.CNContactFamilyNameKey,
            CN.CNContactNicknameKey,
            CN.CNContactOrganizationNameKey,
            CN.CNContactEmailAddressesKey,
            CN.CNContactPhoneNumbersKey,
        ]
        with self._guard:
            store = self._ready()
            predicate = CN.CNContact.predicateForContactsMatchingName_(query)
            found, _error = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
                predicate, keys, None
            )
        people = []
        for contact in found or []:
            name = " ".join(
                part for part in (str(contact.givenName()), str(contact.familyName())) if part
            ) or str(contact.organizationName() or "")
            phones = []
            for item in contact.phoneNumbers():
                label = (
                    CN.CNLabeledValue.localizedStringForLabel_(item.label()) if item.label() else ""
                )
                phones.append((str(label or "phone"), str(item.value().stringValue())))
            people.append(
                Person(
                    name=name,
                    emails=[str(item.value()) for item in contact.emailAddresses()],
                    phones=phones,
                    nickname=str(contact.nickname() or ""),
                    organization=str(contact.organizationName() or ""),
                )
            )
        return people


class ContactsDirectory:
    def __init__(self, backend=None):
        self.backend = backend or ContactsBackend()

    async def search(self, query: str) -> list[Person]:
        return await asyncio.to_thread(self.backend.search, query.strip())

    async def _one(self, name: str) -> list[Person]:
        people = await self.search(name)
        wanted = name.strip().casefold()
        # Prefer exact full-name or nickname matches over partial ones.
        exact = [
            person
            for person in people
            if wanted in {person.name.casefold(), person.nickname.casefold()}
        ]
        return exact or people

    async def email_for(self, value: str) -> str:
        """An address stays as is; a name becomes that person's single email address."""
        value = value.strip()
        if EMAIL.fullmatch(value):
            return value
        people = [person for person in await self._one(value) if person.emails]
        if not people:
            raise ValueError(f"No contact named “{value}” with an email address. Give the address.")
        if len(people) > 1:
            options = "; ".join(f"{person.name} ({person.emails[0]})" for person in people[:5])
            raise ValueError(f"Several contacts match “{value}”: {options}. Say which one.")
        emails = people[0].emails
        if len(emails) > 1:
            raise ValueError(
                f"{people[0].name} has several email addresses: {', '.join(emails[:5])}. "
                "Say which one to use."
            )
        return emails[0]

    async def phone_for(self, value: str) -> str:
        """A phone number or email (iMessage) stays; a name becomes that person's number."""
        value = value.strip()
        if EMAIL.fullmatch(value) or PHONE.fullmatch(value):
            return value
        people = [person for person in await self._one(value) if person.phones or person.emails]
        if not people:
            raise ValueError(f"No contact named “{value}” with a phone number. Give the number.")
        if len(people) > 1:
            options = "; ".join(person.name for person in people[:5])
            raise ValueError(f"Several contacts match “{value}”: {options}. Say which one.")
        person = people[0]
        mobiles = [
            number
            for label, number in person.phones
            if "mobile" in label.casefold() or "iphone" in label.casefold()
        ]
        numbers = mobiles or [number for _label, number in person.phones]
        if len(numbers) == 1:
            return numbers[0]
        if len(numbers) > 1:
            raise ValueError(
                f"{person.name} has several numbers: {', '.join(numbers[:5])}. Say which one."
            )
        return person.emails[0]


class ContactSearchInput(Input):
    query: str = Field(min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")


def render_people(data: dict) -> str:
    if not data["people"]:
        return f"No contacts match “{data['query']}”."
    rows = []
    for person in data["people"]:
        details = [
            *person["emails"],
            *(f"{number} ({label})" for label, number in person["phones"]),
        ]
        rows.append(f"• {person['name']}" + (f" — {', '.join(details)}" if details else ""))
    return "Contacts:\n" + "\n".join(rows)


def register(registry, directory: ContactsDirectory):
    async def find(args):
        people = await directory.search(args.query)
        return {
            "query": args.query,
            "people": [
                {"name": p.name, "emails": p.emails, "phones": p.phones} for p in people[:10]
            ],
            "content_is_untrusted": True,
        }

    registry.register(
        Tool(
            "contacts_find",
            "Look up people in the Mac's Contacts by name: email addresses and phone numbers. "
            "Send tools already accept contact names, so this is only needed to show details.",
            ContactSearchInput,
            RiskLevel.SAFE,
            find,
            render=render_people,
        )
    )


async def emails_from(values: list[str], contacts: ContactsDirectory | None) -> list[str]:
    """Turn a mix of addresses and contact names into distinct, validated addresses."""
    resolved = []
    for value in values:
        value = value.strip()
        if EMAIL.fullmatch(value):
            address = value
        elif contacts is None:
            raise ValueError(f"“{value}” isn't an email address. Give the full address.")
        else:
            address = await contacts.email_for(value)
        if not EMAIL.fullmatch(address):
            raise ValueError(f"Contacts has an invalid email address for “{value}”.")
        if address.casefold() not in {item.casefold() for item in resolved}:
            resolved.append(address)
    return resolved


def email_resolver(contacts: ContactsDirectory | None, *fields: str):
    """A Tool.resolve hook that replaces names in the given list fields with addresses."""

    async def resolve(args):
        updates = {name: await emails_from(getattr(args, name), contacts) for name in fields}
        return args.model_copy(update=updates)

    return resolve
