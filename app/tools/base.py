from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.security.risk import RiskLevel


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


@dataclass
class Tool:
    name: str
    description: str
    input_schema: type[BaseModel]
    risk: RiskLevel
    handler: Callable[[Any], Awaitable[dict]]
    policy: Callable[[Any], RiskLevel] | None = None
    persist_arguments: bool = False
    expose_to_llm: bool = True
    confirmation_message: str | None = None
    # Formats a successful result as readable text for the user. Rendered locally,
    # so it is shown even when the privacy policy withholds the result from the model.
    render: Callable[[dict], str] | None = None

    def validate(self, arguments: dict) -> BaseModel:
        return self.input_schema.model_validate(arguments)

    def classify(self, arguments: BaseModel) -> RiskLevel:
        dynamic = self.policy(arguments) if self.policy else self.risk
        order = list(RiskLevel)
        return max((self.risk, dynamic), key=order.index)

    async def execute(self, arguments: BaseModel) -> dict:
        return await self.handler(arguments)

    def definition(self) -> dict:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema.model_json_schema(),
            "strict": False,
        }
