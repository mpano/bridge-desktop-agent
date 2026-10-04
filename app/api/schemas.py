from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=10000)


class ConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: str = Field(min_length=1, max_length=200)
    approved: bool


class LaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticket: str = Field(min_length=16, max_length=100)


class ChatRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[0-9a-f]{32}$")


class ChatDelete(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    all: bool = False

    @model_validator(mode="after")
    def one_or_all(self):
        if bool(self.id) == self.all:
            raise ValueError("Name one chat, or ask for all of them.")
        return self


class MemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=300)


class ForgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: int = Field(ge=1)


class AgentResponse(BaseModel):
    status: Literal["completed", "failed", "confirmation_required", "cancelled"]
    request_id: str
    message: str
    steps: list[dict[str, Any]]
    confirmation: dict[str, Any] | None = None
