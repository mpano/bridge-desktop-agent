import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.llm.models import LLMProviderError
from app.llm.ollama import (
    MAX_RESPONSE_BYTES,
    OllamaLLMClient,
    convert_history,
    parse_response,
    validate_local_model,
    validate_local_url,
)


def settings(**changes):
    return SimpleNamespace(
        **{
            "local_llm_base_url": "http://127.0.0.1:11434",
            "local_llm_model": "qwen3:8b",
            "local_llm_timeout_seconds": 30,
            **changes,
        }
    )


def reply(**changes):
    return {"done": True, "message": {"role": "assistant", "content": "", **changes}}


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://localhost:11434",
        "http://192.168.1.1",
        "http://127.0.0.1/api",
        "http://user@127.0.0.1",
        "http://127.0.0.1?x=1",
        "http://127.0.0.1#x",
        "http://[::1%lo0]",
        "http://127.0.0.1:0",
        "http://127.1",
    ],
)
def test_reject_nonlocal_or_ambiguous_endpoint(url):
    with pytest.raises(ValueError):
        validate_local_url(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://[::1]:11434/"])
def test_numeric_loopback(url):
    assert validate_local_url(url) == url.rstrip("/")


@pytest.mark.parametrize("model", ["qwen3:cloud", "gpt-oss:120b-cloud", "", " "])
def test_cloud_model_name_rejected(model):
    with pytest.raises(ValueError):
        validate_local_model(model)


@pytest.mark.asyncio
async def test_multi_round_conversion(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://invalid.example:3333")
    requests = []

    def respond(request):
        assert request.url.host == "127.0.0.1"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["tools", "completion"]})
        if len(requests) == 2:
            return httpx.Response(
                200,
                json=reply(
                    tool_calls=[
                        {"function": {"name": "open_app", "arguments": {"app_name": "Spotify"}}},
                        {
                            "function": {
                                "name": "open_url",
                                "arguments": {"url": "https://github.com"},
                            }
                        },
                    ]
                ),
            )
        return httpx.Response(200, json=reply(content="Both actions completed."))

    client = OllamaLLMClient(settings(), transport=httpx.MockTransport(respond))
    history = [{"role": "user", "content": "Open Spotify then GitHub"}]
    definitions = [
        {
            "type": "function",
            "name": "open_app",
            "description": "Open app",
            "parameters": {"type": "object"},
            "strict": True,
        }
    ]
    first = await client.generate_response(history, definitions)
    assert len(first.calls) == 2
    assert first.calls[0].call_id != first.calls[1].call_id
    history.extend(first.output)
    history.extend(
        {"type": "function_call_output", "call_id": call.call_id, "output": '{"success":true}'}
        for call in first.calls
    )
    final = await client.generate_response(history, definitions)
    assert final.text == "Both actions completed."
    messages = requests[-1][1]["messages"]
    assert len(messages[2]["tool_calls"]) == 2
    assert [item["tool_name"] for item in messages[3:]] == ["open_app", "open_url"]
    assert requests[1][1]["stream"] is False
    assert "strict" not in requests[1][1]["tools"][0]["function"]
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"remote_host": "https://ollama.com"},
        {"remote_model": "cloud-alias"},
        {"capabilities": ["completion"]},
        {"capabilities": "tools"},
    ],
)
async def test_remote_metadata_or_missing_tools_prevents_history_transmission(metadata):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=metadata)

    client = OllamaLLMClient(settings(), transport=httpx.MockTransport(respond))
    with pytest.raises(LLMProviderError):
        await client.generate_response([{"role": "user", "content": "private text"}], [])
    assert len(requests) == 1
    assert b"private text" not in requests[0].content
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 404, 500])
async def test_http_errors_and_redirects_do_not_forward_history(status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status, headers={"location": "https://example.com"}, text="secret error body"
        )

    client = OllamaLLMClient(settings(), transport=httpx.MockTransport(respond))
    with pytest.raises(RuntimeError, match=f"HTTP {status}") as error:
        await client.generate_response([], [])
    assert "secret" not in str(error.value)
    assert len(calls) == 1
    await client.close()


@pytest.mark.asyncio
async def test_response_limit():
    client = OllamaLLMClient(
        settings(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b" " * (MAX_RESPONSE_BYTES + 1))
        ),
    )
    with pytest.raises(LLMProviderError):
        await client.generate_response([], [])
    await client.close()


@pytest.mark.asyncio
async def test_connection_failure_is_actionable_and_sanitized():
    def respond(request):
        raise httpx.ConnectError("secret internal details", request=request)

    client = OllamaLLMClient(settings(), transport=httpx.MockTransport(respond))
    with pytest.raises(RuntimeError, match="Start Ollama") as error:
        await client.generate_response([], [])
    assert "secret" not in str(error.value)
    await client.close()


@pytest.mark.parametrize(
    "calls",
    [
        [{"function": {"name": "x", "arguments": "{}"}}],
        [{"function": {"name": "", "arguments": {}}}],
        [{"function": {"name": "x", "arguments": []}}],
        [None],
        [{}] * 33,
    ],
)
def test_malformed_calls_fail_closed(calls):
    with pytest.raises(ValueError):
        parse_response(reply(tool_calls=calls))


def test_prose_never_becomes_action():
    result = parse_response(reply(content='{"name":"open_app","arguments":{}}'))
    assert result.calls == []


def test_history_orphan_and_duplicate_output_rejected():
    with pytest.raises(ValueError, match="Orphan"):
        convert_history([{"type": "function_call_output", "call_id": "unknown", "output": "ok"}])
    call = {"type": "function_call", "call_id": "a", "name": "x", "arguments": "{}"}
    result = {"type": "function_call_output", "call_id": "a", "output": "ok"}
    with pytest.raises(ValueError, match="duplicate"):
        convert_history([call, result, result])


@pytest.mark.asyncio
async def test_overall_timeout_and_cancellation_propagation():
    entered = asyncio.Event()

    async def respond(request):
        entered.set()
        await asyncio.Event().wait()

    client = OllamaLLMClient(
        settings(local_llm_timeout_seconds=0.02), transport=httpx.MockTransport(respond)
    )
    with pytest.raises(LLMProviderError, match="timed out"):
        await client.generate_response([], [])
    client.timeout = 10
    entered.clear()
    task = asyncio.create_task(client.generate_response([], []))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.close()
