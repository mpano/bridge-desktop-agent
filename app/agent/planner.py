from app.llm.client import LLMClient
from app.security.privacy import ToolResultPrivacy
from app.tools.registry import ToolRegistry


class Planner:
    def __init__(
        self, llm: LLMClient, registry: ToolRegistry, privacy: ToolResultPrivacy | None = None
    ):
        self.llm, self.registry = llm, registry
        self.privacy = privacy or ToolResultPrivacy()

    async def plan(self, history, on_text=None):
        history, tools = self.privacy.filter_history(history), self.registry.definitions()
        if on_text is not None and getattr(self.llm, "streams", False) is True:
            return await self.llm.generate_response(history, tools, on_text=on_text)
        return await self.llm.generate_response(history, tools)
