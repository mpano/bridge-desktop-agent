"""What a request cost, from what OpenAI reported (the same prices the Mac app shows)."""

from __future__ import annotations

from app.llm.usage import PRICES, price_of

from .store import MICRO


def allowed(model: str) -> bool:
    return price_of(str(model or "")) is not None


def usage_micros(model: str, usage: dict | None) -> int | None:
    """Responses API or Chat Completions `usage` → micro-dollars (None if unknown)."""
    price = price_of(model)
    if price is None or not isinstance(usage, dict):
        return None
    inp = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    out = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached = int((details or {}).get("cached_tokens", 0) or 0)
    dollars = ((inp - cached) * price[0] + cached * price[1] + out * price[2]) / 1_000_000
    return max(1, round(dollars * MICRO))


def guess_micros(model: str, request_bytes: int, max_output: int | None) -> int:
    """When OpenAI's usage never arrived (the Mac hung up mid-answer): a fair estimate."""
    price = price_of(model) or max(PRICES.values())
    tokens_in = request_bytes / 4
    tokens_out = min(max_output or 1000, 4000)
    return max(1, round((tokens_in * price[0] + tokens_out * price[2]) / 1_000_000 * MICRO))
