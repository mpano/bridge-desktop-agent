from app.tools.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ValueError("Unknown tool requested")
        return self._tools[name]

    def definitions(self) -> list[dict]:
        return [tool.definition() for tool in self._tools.values() if tool.expose_to_llm]

    def catalog(self) -> list[dict]:
        """Describe public capabilities without evaluating or executing any tool."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "risk": tool.risk.value,
                "dynamic_policy": tool.policy is not None,
                "input_schema": tool.input_schema.model_json_schema(),
                "arguments_persisted": tool.persist_arguments,
                "confirmation_message": tool.confirmation_message,
            }
            for tool in sorted(self._tools.values(), key=lambda tool: tool.name)
            if tool.expose_to_llm
        ]
