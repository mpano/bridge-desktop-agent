from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict


class LLMProviderError(RuntimeError):
    """An actionable provider error safe to display (never raw response bodies)."""


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    calls: list[ToolCall] = field(default_factory=list)
    text: str = ""
    output: list[dict] = field(default_factory=list)
