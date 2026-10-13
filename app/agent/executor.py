import time

import structlog
from pydantic import ValidationError

from app.llm.models import ToolCall
from app.security.permissions import PolicyError, SecurityPolicy
from app.security.risk import RiskLevel
from app.tools.registry import ToolRegistry

log = structlog.get_logger()


class Executor:
    def __init__(self, registry: ToolRegistry, policy: SecurityPolicy | None = None):
        self.registry = registry
        self.policy = policy or SecurityPolicy()
        self.actions = None  # What Bridge did, for Activity and Undo (set by bootstrap).

    async def execute(self, call: ToolCall, request_id: str, approved: bool = False) -> dict:
        started = time.monotonic()
        result = {"tool": call.name, "call_id": call.call_id, "success": False}
        selected_tool = "unknown"
        try:
            tool = self.registry.get(call.name)
            selected_tool = tool.name
            arguments = tool.validate(call.arguments)
            if tool.resolve is not None:
                arguments = await tool.resolve(arguments)
            risk = self.policy.evaluate(tool, arguments)
            if risk == RiskLevel.CONFIRM and not approved:
                result.update(status="confirmation_required", arguments=arguments.model_dump())
            else:
                data = await tool.execute(arguments)
                if isinstance(data, dict) and "_privacy_key" in data:
                    # Whose result this is, for the privacy rules (e.g. one MCP app's).
                    data = dict(data)
                    result["privacy_key"] = str(data.pop("_privacy_key"))
                result.update(status="completed", success=True, result=data)
                if self.actions is not None and request_id != "undo":
                    # Written down for Activity, with Undo where the service allows it.
                    try:
                        noted = self.actions.record(
                            tool.name, arguments.model_dump(), data, request_id
                        )
                    except Exception:
                        noted = None
                    if noted and noted["undoable"]:
                        result["undo_id"] = noted["id"]
                if tool.render is not None:
                    try:
                        display = tool.render(data)
                    except Exception:
                        display = None
                    if display:
                        result["display"] = display
        except ValidationError:
            result.update(status="failed", error="Invalid tool arguments.")
        except (PolicyError, ValueError, RuntimeError) as exc:
            result.update(status="failed", error=str(exc))
        except Exception:
            result.update(status="failed", error="Operation failed; inspect local configuration.")
        log.info(
            "tool_execution",
            request_id=request_id,
            tool=selected_tool,
            duration_ms=round((time.monotonic() - started) * 1000),
            success=result["success"],
            status=result["status"],
        )
        return result
