from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator

from app.tools.base import Input

Address = Annotated[
    str,
    Field(
        min_length=3,
        max_length=254,
        pattern=r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$",
    ),
]
# An email address, or a contact's name that is resolved to one before approval. Commas,
# angle brackets and control characters can never reach an email header.
Recipient = Annotated[
    str,
    Field(min_length=1, max_length=254, pattern=r"^[^\x00-\x1f\x7f,;<>]+$"),
]
ResourceID = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_@.:-]+$")]


class AccountInput(Input):
    account_id: str | None = Field(
        default=None,
        min_length=3,
        max_length=512,
        pattern=r"^[^\x00-\x1f\x7f]+$",
        description="Omit to use the only connected account; or give its email/identity.",
    )


class SearchInput(AccountInput):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=10)


class ThreadInput(AccountInput):
    thread_id: str = Field(pattern=r"^[a-fA-F0-9]{1,64}$")


class SendEmailInput(AccountInput):
    to: list[Recipient] = Field(
        min_length=1, max_length=10, description="Email addresses or names from Contacts"
    )
    subject: str = Field(min_length=1, max_length=500, pattern=r"^[^\r\n\x00]+$")
    body: str = Field(min_length=1, max_length=20000)
    thread_id: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{1,64}$")
    in_reply_to: str | None = Field(default=None, max_length=500, pattern=r"^<[^<>\s]+>$")

    @model_validator(mode="after")
    def reply_headers(self):
        if bool(self.thread_id) != bool(self.in_reply_to):
            raise ValueError("Replies require both thread_id and the original Message-ID.")
        return self


SlackDestination = Annotated[
    str,
    Field(
        min_length=1,
        max_length=120,
        pattern=r"^[^\x00-\x1f\x7f]+$",
        validation_alias=AliasChoices("channel", "channel_id"),
        description="Channel name like #general, a person like @alex or 'Alex Kim', or an exact ID",
    ),
]


class SlackMessageInput(AccountInput):
    channel: SlackDestination
    text: str = Field(min_length=1, max_length=10000)
    thread_ts: str | None = Field(default=None, pattern=r"^[0-9]{10,16}\.[0-9]{6}$")


class SlackHistoryInput(AccountInput):
    channel: SlackDestination
    limit: int = Field(default=15, ge=1, le=50)


class CalendarWindowInput(AccountInput):
    calendar_id: ResourceID = "primary"
    start: str = Field(max_length=40, description="ISO 8601 date-time with explicit UTC offset")
    end: str = Field(max_length=40, description="ISO 8601 date-time with explicit UTC offset")

    @field_validator("start", "end")
    @classmethod
    def offset_required(cls, value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if "T" not in value or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Use a full date-time with UTC offset.")
        return value

    @model_validator(mode="after")
    def valid_window(self):
        start = datetime.fromisoformat(self.start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(self.end.replace("Z", "+00:00"))
        if end <= start or end - start > timedelta(days=90):
            raise ValueError("Choose an increasing time window of at most 90 days.")
        return self


class FreeTimeInput(CalendarWindowInput):
    min_minutes: int = Field(default=30, ge=5, le=480)


class DeleteEventInput(CalendarWindowInput):
    title: str = Field(min_length=1, max_length=500, description="Exact event title")
    send_updates: Literal["all", "none"] = "all"


class CreateEventInput(CalendarWindowInput):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=10000)
    attendees: list[Recipient] = Field(
        default_factory=list, max_length=20, description="Email addresses or contact names"
    )
    send_updates: Literal["all", "none"] = "all"


class SpotifySearchInput(SearchInput):
    kind: Literal["track", "playlist", "album", "artist"] = "track"


class SpotifyPlayInput(AccountInput):
    uri: str = Field(pattern=r"^spotify:(track|playlist|album|artist):[A-Za-z0-9]{22}$")
    device_id: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$"
    )


class SpotifyQueueInput(SpotifyPlayInput):
    uri: str = Field(pattern=r"^spotify:track:[A-Za-z0-9]{22}$")
