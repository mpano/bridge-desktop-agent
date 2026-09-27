"""Deterministic tool-result disclosure rules, independent of model instructions."""

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal

RemoteResultMode = Literal["status_only", "allowlist", "all"]
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_STATUSES = frozenset({"completed", "failed", "confirmation_required", "cancelled", "blocked"})


@dataclass(frozen=True)
class ToolResultPrivacy:
    """Local Ollama keeps results local; all other providers use remote rules.

    This controls tool output, not user-authored prompts or prior disclosures.
    Allowlisting a tool permits its complete results, including errors and paths.
    """

    provider: str
    mode: RemoteResultMode = "status_only"
    allowed_tools: frozenset[str] | set[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.mode not in {"status_only", "allowlist", "all"}:
            raise ValueError("Unknown remote tool-result privacy mode.")
        object.__setattr__(self, "allowed_tools", frozenset(self.allowed_tools))

    def filter_result(self, result: dict) -> dict:
        """Return a detached result with only explicitly permitted data."""
        if not isinstance(result, dict):
            return {"success": False, "status": "failed", "result_withheld": True}
        tool = result.get("tool")
        if (
            self.provider == "ollama"
            or self.mode == "all"
            or (self.mode == "allowlist" and isinstance(tool, str) and tool in self.allowed_tools)
        ):
            return deepcopy(result)
        filtered: dict = {"result_withheld": True}
        for key in ("tool", "call_id"):
            value = result.get(key)
            if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
                filtered[key] = value
        filtered["success"] = result.get("success") is True
        status = result.get("status")
        filtered["status"] = status if isinstance(status, str) and status in _STATUSES else "failed"
        return filtered

    def summary(self, steps: list[dict]) -> str:
        return json.dumps([self.filter_result(step) for step in steps])

    def filter_history(self, history: list[dict]) -> list[dict]:
        """Filter structured tool outputs again at the provider boundary.

        Ordinary assistant prose and user messages are deliberately unchanged.
        The agent's JSON step summaries are recognized by their tool fields.
        Malformed function output is withheld rather than passed through.
        """
        filtered = deepcopy(history)
        for item in filtered:
            if item.get("type") == "function_call_output":
                try:
                    result = json.loads(item.get("output", ""))
                except (ValueError, TypeError):
                    result = None
                item["output"] = json.dumps(self.filter_result(result))
            elif item.get("role") == "assistant" and isinstance(item.get("content"), str):
                try:
                    steps = json.loads(item["content"])
                except ValueError:
                    continue
                if (
                    isinstance(steps, list)
                    and steps
                    and all(isinstance(step, dict) and "tool" in step for step in steps)
                ):
                    item["content"] = self.summary(steps)
        return filtered
