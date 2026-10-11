import base64
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
        self.usage = None  # Routing and spending (app/llm/usage.py), set by bootstrap.

    def _model(self) -> tuple[str, str]:
        from app.llm.usage import current_task

        name = current_task()
        model = self.usage.model_for(name) if self.usage is not None else self.settings.openai_model
        return name, model

    def _record(self, name: str, model: str, response) -> None:
        if self.usage is not None:
            try:
                self.usage.record_response(name, model, getattr(response, "usage", None))
            except Exception:
                pass  # Counting must never break a request.

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
        name, model = self._model()
        response = await self._openai().responses.create(
            model=model,
            instructions=instructions,
            input=[{"role": "user", "content": text}],
            store=False,
        )
        self._record(name, model, response)
        return response.output_text.strip()

    async def describe_image(self, instructions: str, png: bytes) -> str:
        """One plain text answer about a PNG image. Nothing is stored."""
        image = "data:image/png;base64," + base64.b64encode(png).decode()
        name, model = self._model()
        response = await self._openai().responses.create(
            model=model,
            instructions=instructions,
            input=[{"role": "user", "content": [{"type": "input_image", "image_url": image}]}],
            store=False,
        )
        self._record(name, model, response)
        return response.output_text.strip()

    async def generate_response(self, history, tools):
        self._openai()
        name, model = self._model()
        response = await self._client.responses.create(
            model=model,
            instructions=system_prompt(),
            input=history,
            tools=tools,
            parallel_tool_calls=False,
            store=False,
        )
        self._record(name, model, response)
        output = [item.model_dump(exclude_none=True) for item in response.output]
        return LLMResponse(parse_tool_calls(output), response.output_text, output)

    async def close(self):
        if self._client:
            await self._client.close()
