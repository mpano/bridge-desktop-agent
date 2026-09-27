from app.llm.client import LLMClient
from app.security.privacy import ToolResultPrivacy
from app.tools.registry import ToolRegistry


class Planner:
    def __init__(
        self, llm: LLMClient, registry: ToolRegistry, privacy: ToolResultPrivacy | None = None
    ):
        self.llm, self.registry = llm, registry
        self.privacy = privacy or ToolResultPrivacy(provider="openai")

    async def plan(self, history):
        return await self.llm.generate_response(
            self.privacy.filter_history(history), self.registry.definitions()
        )
