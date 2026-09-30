import json
from typing import Protocol

from openai import AsyncOpenAI

from app.llm.models import LLMResponse, ToolCall
from app.llm.prompts import system_prompt


class LLMClient(Protocol):
    async def generate_response(self, history: list[dict], tools: list[dict]) -> LLMResponse: ...


def parse_tool_calls(output: list[dict]) -> list[ToolCall]:
    calls = []
    for item in output:
        if item.get("type") == "function_call":
            arguments = json.loads(item["arguments"])
            calls.append(ToolCall(call_id=item["call_id"], name=item["name"], arguments=arguments))
    if len({call.call_id for call in calls}) != len(calls):
        raise ValueError("Duplicate tool call IDs.")
    return calls


class OpenAILLMClient:
    def __init__(self, settings):
        self.settings = settings
        self._client = None

    def _openai(self):
        if not self.settings.openai_api_key.get_secret_value():
            raise RuntimeError("Set OPENAI_API_KEY in .env to use the agent.")
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.settings.openai_api_key.get_secret_value(),
                timeout=45,
                max_retries=1,
            )
        return self._client

    async def complete(self, instructions: str, text: str) -> str:
        """One plain text answer: no tools, no conversation history, nothing stored."""
        response = await self._openai().responses.create(
            model=self.settings.openai_model,
            instructions=instructions,
            input=[{"role": "user", "content": text}],
            store=False,
        )
        return response.output_text.strip()

    async def generate_response(self, history, tools):
        self._openai()
        response = await self._client.responses.create(
            model=self.settings.openai_model,
            instructions=system_prompt(),
            input=history,
            tools=tools,
            parallel_tool_calls=False,
            store=False,
        )
        output = [item.model_dump(exclude_none=True) for item in response.output]
        return LLMResponse(parse_tool_calls(output), response.output_text, output)

    async def close(self):
        if self._client:
            await self._client.close()
