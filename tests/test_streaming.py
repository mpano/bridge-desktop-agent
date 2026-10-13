"""Answers in Ask arrive word by word. OpenAI is faked; nothing leaves the machine."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config.settings import Settings
from app.llm.client import OpenAILLMClient
from app.llm.models import LLMResponse


class FakeStream:
    def __init__(self, words, final):
        self.words, self.final = words, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def events():
            for word in self.words:
                yield SimpleNamespace(type="response.output_text.delta", delta=word)
            yield SimpleNamespace(type="response.completed", delta=None)

        return events()

    async def get_final_response(self):
        return self.final


def final(text, output=()):
    items = [
        SimpleNamespace(model_dump=lambda exclude_none=True, item=item: item) for item in output
    ]
    return SimpleNamespace(output=items, output_text=text, usage=None)


async def test_the_client_hands_over_words_as_they_arrive(tmp_path):
    client = OpenAILLMClient(
        Settings(_env_file=None, database_path=tmp_path / "db", openai_api_key="sk-x")
    )
    seen = []
    streamed = FakeStream(["Hel", "lo ", "there"], final("Hello there"))
    client._client = SimpleNamespace(
        responses=SimpleNamespace(stream=lambda **kw: streamed, create=AsyncMock())
    )
    reply = await client.generate_response(
        [{"role": "user", "content": "hi"}], [], on_text=seen.append
    )
    assert seen == ["Hel", "lo ", "there"] and reply.text == "Hello there"
    client._client.responses.create.assert_not_awaited()

    def broken(_words):
        raise RuntimeError("the screen went away")

    call = {"type": "function_call", "call_id": "c1", "name": "look_up", "arguments": "{}"}
    tools = FakeStream(["Let me check"], final("", [call]))
    client._client.responses.stream = lambda **kw: tools
    reply = await client.generate_response([], [], on_text=broken)  # Never breaks the answer.
    assert reply.calls[0].name == "look_up"


async def test_a_request_shows_the_answer_growing(tmp_path):
    from app.bootstrap import build_agent

    words = ["You ", "owe ", "Ana ", "the deck."]

    class StreamingLLM:
        streams = True

        def __init__(self):
            self.partials = []

        async def generate_response(self, history, tools, on_text=None):
            for word in words:
                on_text(word)
                self.partials.append(progress())
                await asyncio.sleep(0)
            return LLMResponse(text="".join(words))

    llm = StreamingLLM()
    agent = build_agent(
        Settings(_env_file=None, database_path=tmp_path / "db", openai_api_key="sk-x"), llm=llm
    )
    started = agent.submit("What do I owe Ana?")
    progress = lambda: agent.task_progress(started["request_id"])["partial"]  # noqa: E731
    await agent.background
    assert llm.partials == ["You ", "You owe ", "You owe Ana ", "You owe Ana the deck."]
    done = agent.task_progress(started["request_id"])
    assert done["result"]["message"] == "You owe Ana the deck." and done["partial"] == ""


async def test_models_that_cant_stream_are_asked_the_usual_way(tmp_path):
    from app.agent.planner import Planner
    from app.tools.registry import ToolRegistry

    llm = SimpleNamespace(generate_response=AsyncMock(return_value=LLMResponse(text="ok")))
    planner = Planner(llm, ToolRegistry())
    await planner.plan([{"role": "user", "content": "hi"}], on_text=lambda words: None)
    assert "on_text" not in llm.generate_response.await_args.kwargs
