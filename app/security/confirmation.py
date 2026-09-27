import secrets
import time
from dataclasses import dataclass

from app.agent.context import AgentContext
from app.llm.models import ToolCall


@dataclass
class Pending:
    context: AgentContext
    call: ToolCall
    expires: float


class ConfirmationStore:
    def __init__(self, ttl: float = 300):
        self.ttl = ttl
        self._pending: dict[str, Pending] = {}

    def create(self, context: AgentContext, call: ToolCall) -> str:
        now = time.monotonic()
        self._pending = {k: v for k, v in self._pending.items() if v.expires > now}
        if len(self._pending) >= 100:
            raise ValueError("Too many pending confirmations.")
        token = secrets.token_urlsafe(32)
        self._pending[token] = Pending(context, call.model_copy(deep=True), now + self.ttl)
        return token

    def consume(self, token: str) -> Pending:
        pending = self._pending.pop(token, None)
        if pending is None or pending.expires < time.monotonic():
            raise ValueError("Confirmation expired, unknown, or already used.")
        return pending

    def invalidate(self, request_id: str) -> None:
        self._pending = {
            token: pending
            for token, pending in self._pending.items()
            if pending.context.request_id != request_id
        }
