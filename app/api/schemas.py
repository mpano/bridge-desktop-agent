from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
