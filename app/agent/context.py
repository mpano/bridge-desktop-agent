from dataclasses import dataclass, field
from uuid import uuid4

from app.llm.models import ToolCall


@dataclass
class AgentContext:
    request_id: str = field(default_factory=lambda: uuid4().hex)
    history: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    queue: list[ToolCall] = field(default_factory=list)
    rounds: int = 0
    recovering: bool = False
    cancel_requested: bool = False
    from_phone: bool = False  # Asked from the phone: nothing that reads this Mac's screen.
    current_tool: str | None = None
    confirmation_message: str | None = None
    completion_message: str = (
        "Saved steps finished. Any work not yet planned before interruption needs a new request."
    )
