"""Native Ollama adapter. Local service configuration remains a trusted boundary."""

import asyncio
import ipaddress
import json
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.llm.models import LLMProviderError, LLMResponse, ToolCall
from app.llm.prompts import system_prompt

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def validate_local_url(value: str) -> str:
    """Accept only numeric loopback HTTP endpoints, without URL credentials or paths."""
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
        valid = (
            parsed.scheme == "http"
            and address.is_loopback
            and not parsed.username
            and not parsed.password
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and (port is None or port > 0)
            and not any(char.isspace() for char in value)
            and "%" not in value
            and "@" not in value
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("LOCAL_LLM_BASE_URL must be an HTTP numeric loopback URL without a path.")
    return value.rstrip("/")


def validate_local_model(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("Configure a nonempty local Ollama model name.")
    if ":cloud" in value.lower() or "-cloud" in value.lower():
        raise ValueError("Cloud models are not allowed by the local provider.")
    return value


def _content(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(
        isinstance(part, dict)
        and part.get("type") in ("output_text", "input_text")
        and isinstance(part.get("text"), str)
        for part in value
    ):
        return "\n".join(part["text"] for part in value)
    raise ValueError("Unsupported conversation content for the local provider.")


def convert_history(history: list[dict]) -> list[dict]:
    messages = [{"role": "system", "content": system_prompt()}]
    pending: dict[str, str] = {}
    seen = set()
    previous_call = False
    for item in history:
        kind = item.get("type")
        if kind == "function_call":
            call_id, name = item.get("call_id"), item.get("name")
            arguments = item.get("arguments")
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in seen
                or not isinstance(name, str)
                or not name
                or not isinstance(arguments, dict)
            ):
                raise ValueError("Invalid historical tool call.")
            if not previous_call:
                if pending:
                    raise ValueError("Missing historical tool results.")
                messages.append({"role": "assistant", "content": "", "tool_calls": []})
            messages[-1]["tool_calls"].append(
                {"type": "function", "function": {"name": name, "arguments": arguments}}
            )
            pending[call_id] = name
            seen.add(call_id)
            previous_call = True
        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if call_id not in pending:
                raise ValueError("Orphan or duplicate historical tool result.")
            messages.append(
                {
                    "role": "tool",
                    "tool_name": pending.pop(call_id),
                    "content": _content(item.get("output")),
                }
            )
            previous_call = False
        elif kind == "reasoning":
            continue
        else:
            if pending:
                raise ValueError("Missing historical tool results.")
            if item.get("role") not in ("user", "assistant"):
                raise ValueError("Unsupported conversation role for the local provider.")
            messages.append({"role": item["role"], "content": _content(item.get("content"))})
            previous_call = False
    if pending:
        raise ValueError("Missing historical tool results.")
    return messages


def parse_response(payload: dict) -> LLMResponse:
    message = payload.get("message")
    if payload.get("done") is not True or not isinstance(message, dict):
        raise ValueError("Ollama returned an incomplete chat response.")
    if message.get("role") != "assistant" or not isinstance(message.get("content", ""), str):
        raise ValueError("Ollama returned an invalid assistant message.")
    raw_calls = message.get("tool_calls", [])
    if not isinstance(raw_calls, list) or len(raw_calls) > 32:
        raise ValueError("Ollama returned an invalid tool call list (maximum 32).")
    calls, output = [], []
    text = message.get("content", "")
    if text:
        output.append({"role": "assistant", "content": text})
    for raw in raw_calls:
        function = raw.get("function") if isinstance(raw, dict) else None
        if (
            not isinstance(function, dict)
            or not isinstance(function.get("name"), str)
            or not function["name"].strip()
            or not isinstance(function.get("arguments"), dict)
        ):
            raise ValueError("Ollama returned malformed structured tool arguments.")
        call = ToolCall(call_id=uuid4().hex, name=function["name"], arguments=function["arguments"])
        calls.append(call)
        output.append(
            {
                "type": "function_call",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": json.dumps(call.arguments, allow_nan=False),
            }
        )
    return LLMResponse(calls=calls, text=text, output=output)


class OllamaLLMClient:
    def __init__(self, settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = validate_local_url(settings.local_llm_base_url)
        self.model = validate_local_model(settings.local_llm_model)
        self.timeout = settings.local_llm_timeout_seconds
        self._client = httpx.AsyncClient(
            timeout=settings.local_llm_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    async def _post(self, endpoint: str, payload: dict) -> dict:
        try:
            async with self._client.stream(
                "POST", self.base_url + endpoint, json=payload
            ) as response:
                if response.status_code != 200:
                    raise LLMProviderError(
                        f"Ollama returned HTTP {response.status_code}; check the local service "
                        "and installed model. No remote fallback was attempted."
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise ValueError("Ollama response exceeded the size limit.")
            result = json.loads(data)
            if not isinstance(result, dict) or "error" in result:
                raise ValueError("Ollama returned an invalid response.")
            return result
        except httpx.HTTPError:
            raise LLMProviderError(
                "Cannot reach the local Ollama service or the request timed out. "
                "Start Ollama and check the configured model. No remote fallback was attempted."
            ) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Ollama returned invalid JSON.") from None

    async def generate_response(self, history: list[dict], tools: list[dict]) -> LLMResponse:
        try:
            async with asyncio.timeout(self.timeout):
                return await self._generate_response(history, tools)
        except TimeoutError:
            raise LLMProviderError(
                "Local Ollama request timed out. Check the model and timeout setting."
            ) from None
        except ValueError:
            raise LLMProviderError(
                "Ollama model metadata, response, or conversation is invalid. Ensure a local "
                "tool-capable model is installed; cloud models are refused."
            ) from None

    async def _generate_response(self, history: list[dict], tools: list[dict]) -> LLMResponse:
        messages = convert_history(history)
        definitions = [
            {
                "type": "function",
                "function": {key: tool[key] for key in ("name", "description", "parameters")},
            }
            for tool in tools
        ]
        # Recheck on every round: the local server may change a model's alias.
        info = await self._post("/api/show", {"model": self.model})
        if info.get("remote_host") or info.get("remote_model"):
            raise ValueError("Ollama reports a remote model; local mode refuses to send history.")
        capabilities = info.get("capabilities")
        if capabilities is not None and (
            not isinstance(capabilities, list) or "tools" not in capabilities
        ):
            raise ValueError("The configured Ollama model does not support tool calling.")
        result = await self._post(
            "/api/chat",
            {
                "model": self.model,
                "messages": messages,
                "tools": definitions,
                "stream": False,
                "think": False,
            },
        )
        return parse_response(result)

    async def close(self):
        await self._client.aclose()
