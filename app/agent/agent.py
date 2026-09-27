import asyncio
import json
import sqlite3
from uuid import uuid4

from app.agent.context import AgentContext
from app.agent.executor import Executor
from app.agent.planner import Planner
from app.llm.models import LLMProviderError, ToolCall
from app.preferences.service import ApplicationPreferences
from app.security.confirmation import ConfirmationStore, Pending
from app.security.permissions import checked_path
from app.workflows.retention import PruneInput, RetentionPolicy, WorkflowQuery
from app.workflows.store import WorkflowRepository


class Agent:
    def __init__(
        self,
        planner: Planner,
        executor: Executor,
        max_rounds: int = 12,
        workflows: WorkflowRepository | None = None,
        preferences: ApplicationPreferences | None = None,
    ):
        self.planner, self.executor = planner, executor
        self.max_rounds = max_rounds
        self.workflows = workflows
        self.preferences = preferences
        self.confirmations = ConfirmationStore()
        self.lock = asyncio.Lock()
        self.history: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.background: asyncio.Task | None = None

    def submit(self, message: str) -> dict:
        """Start one tracked request; never create an unbounded execution queue."""
        if self.background and not self.background.done():
            raise ValueError("A background request is already active.")
        context = AgentContext()
        return self._start_background(context, message=message)

    def submit_confirmation(self, token: str, approved: bool) -> dict:
        # Check capacity before consuming the single-use approval.
        if self.background and not self.background.done():
            raise ValueError("A background request is already active.")
        pending = self.confirmations.consume(token)
        return self._start_background(pending.context, pending=pending, approved=approved)

    def _start_background(
        self,
        context: AgentContext,
        *,
        message: str | None = None,
        pending: Pending | None = None,
        approved: bool = False,
    ) -> dict:
        state = {"context": context, "status": "queued", "result": None}
        self.tasks.pop(context.request_id, None)
        self.tasks[context.request_id] = state
        while len(self.tasks) > 100:
            del self.tasks[next(iter(self.tasks))]
        self.background = asyncio.create_task(
            self._run_background(message, context, state, pending, approved)
        )
        return self.task_progress(context.request_id)

    async def _run_background(
        self,
        message: str | None,
        context: AgentContext,
        state: dict,
        pending: Pending | None = None,
        approved: bool = False,
    ) -> None:
        try:
            async with self.lock:
                if message is not None:
                    context.history = [*self.history, {"role": "user", "content": message}]
                state["status"] = "running"
                try:
                    state["result"] = (
                        await self._continue_confirmation(pending, approved)
                        if pending
                        else await self._advance(context)
                    )
                except (sqlite3.Error, OSError):
                    state["result"] = self._storage_failure(context)
        except Exception:
            state["result"] = {
                "status": "failed",
                "request_id": context.request_id,
                "steps": context.steps,
                "message": "Execution stopped unexpectedly. Review the workflow before retrying.",
            }
        finally:
            state["status"] = (state["result"] or {}).get("status", "interrupted")

    def task_progress(self, request_id: str) -> dict:
        if request_id not in self.tasks:
            raise ValueError("Unknown live task. Review saved workflows after a restart.")
        state = self.tasks[request_id]
        context = state["context"]
        return {
            "request_id": request_id,
            "status": state["status"],
            "cancel_requested": context.cancel_requested,
            "completed_steps": len(context.steps),
            "planning_round": context.rounds,
            "current_tool": context.current_tool,
            "remaining_tools": [call.name for call in context.queue],
            "result": state["result"],
        }

    def list_tasks(self) -> list[dict]:
        """Discovery metadata only: results and approval tokens require individual lookup."""
        return [
            {key: value for key, value in self.task_progress(request_id).items() if key != "result"}
            for request_id in reversed(self.tasks)
        ]

    def capabilities(self) -> list[dict]:
        return self.executor.registry.catalog()

    def stop_task(self, request_id: str) -> dict:
        progress = self.task_progress(request_id)
        if progress["status"] not in {"queued", "running"}:
            raise ValueError(
                "Task is no longer running. Use workflow controls for pending approvals."
            )
        self.tasks[request_id]["context"].cancel_requested = True
        return self.task_progress(request_id)

    def _cancelled(self, context: AgentContext) -> dict:
        context.queue.clear()
        return self.response(
            context, "cancelled", "Remaining work cancelled. Completed actions were not undone."
        )

    def response(self, context: AgentContext, status: str, message: str, **extra) -> dict:
        if self.workflows and status in {"completed", "failed", "cancelled"}:
            self.workflows.save(context, status)
        result = {
            "status": status,
            "request_id": context.request_id,
            "message": message,
            "steps": context.steps,
            **extra,
        }
        if context.request_id in self.tasks:
            self.tasks[context.request_id].update(status=status, result=result)
        return result

    def _storage_failure(self, context: AgentContext) -> dict:
        # Never overwrite an in-flight marker if its post-operation commit failed.
        return {
            "status": "failed",
            "request_id": context.request_id,
            "steps": context.steps,
            "message": "Workflow storage failed. Execution stopped; the last action's "
            "outcome may be uncertain. Review it before starting a new request.",
        }

    async def message(self, message: str) -> dict:
        async with self.lock:
            context = AgentContext(history=[*self.history, {"role": "user", "content": message}])
            try:
                return await self._advance(context)
            except (sqlite3.Error, OSError):
                return self._storage_failure(context)

    async def confirm(self, token: str, approved: bool) -> dict:
        async with self.lock:
            pending = self.confirmations.consume(token)
            return await self._continue_confirmation(pending, approved)

    async def _continue_confirmation(self, pending: Pending, approved: bool) -> dict:
        context = pending.context
        try:
            if context.cancel_requested:
                return self._cancelled(context)
            if not approved:
                return self.response(
                    context, "cancelled", "Action declined; remaining steps cancelled."
                )
            result = await self._execute(context, pending.call, approved=True)
            self._record(context, pending.call, result)
            if not result["success"]:
                return self.response(context, "failed", result["error"])
            return await self._advance(context)
        except (sqlite3.Error, OSError):
            return self._storage_failure(context)

    async def _execute(self, context: AgentContext, call: ToolCall, approved: bool = False) -> dict:
        if self.workflows:
            self.workflows.save(context, "running", in_flight=True, queue=[call, *context.queue])
        context.current_tool = call.name
        try:
            result = await self.executor.execute(call, context.request_id, approved=approved)
        finally:
            context.current_tool = None
        if self.workflows and result["status"] == "confirmation_required":
            self.workflows.save(context, "confirmation_required", queue=[call, *context.queue])
        return result

    def _record(self, context: AgentContext, call: ToolCall, result: dict) -> None:
        context.steps.append(result)
        context.history.append(
            {"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(result)}
        )
        if self.workflows:
            self.workflows.save(context, "running" if result["success"] else "failed")

    def _prepare(self, calls: list[ToolCall], *, allow_private: bool = False) -> list[ToolCall]:
        prepared = []
        for call in calls:
            tool = self.executor.registry.get(call.name)
            if not tool.expose_to_llm and not allow_private:
                raise ValueError("This tool requires a trusted management request.")
            arguments = tool.validate(call.arguments)
            self.executor.policy.evaluate(tool, arguments)
            values = arguments.model_dump()
            if self.preferences:
                if call.name == "open_project" and not values.get("editor"):
                    values["editor"] = self.preferences.editor()
                if call.name == "open_url" and not values.get("browser"):
                    values["browser"] = self.preferences.browser()
            # Freeze relative paths before approval/checkpoint so a restart cannot reinterpret them.
            for key in ("path", "cwd"):
                if values.get(key) is not None:
                    values[key] = str(checked_path(values[key]))
            if call.name == "run_terminal_command" and values.get("cwd") is None:
                values["cwd"] = str(checked_path("."))
            prepared.append(call.model_copy(update={"arguments": values}, deep=True))
        return prepared

    async def _advance(self, context: AgentContext) -> dict:
        while True:
            if context.cancel_requested:
                return self._cancelled(context)
            while context.queue:
                if context.cancel_requested:
                    return self._cancelled(context)
                if len(context.steps) >= 32:
                    return self.response(context, "failed", "Agent reached the 32-action limit.")
                call = context.queue.pop(0)
                result = await self._execute(context, call)
                if context.cancel_requested and result["status"] == "confirmation_required":
                    return self._cancelled(context)
                if result["status"] == "confirmation_required":
                    try:
                        token = self.confirmations.create(context, call)
                    except ValueError as exc:
                        return self.response(context, "failed", str(exc))
                    return self.response(
                        context,
                        "confirmation_required",
                        context.confirmation_message
                        or self.executor.registry.get(call.name).confirmation_message
                        or f"Allow {call.name}?",
                        confirmation={
                            "token": token,
                            "action": call.name,
                            "arguments": result["arguments"],
                            "expires_in_seconds": 300,
                        },
                    )
                self._record(context, call, result)
                if not result["success"]:
                    return self.response(context, "failed", result["error"])
            if context.cancel_requested:
                return self._cancelled(context)
            if context.recovering:
                return self.response(
                    context,
                    "completed",
                    context.completion_message,
                )
            if context.rounds >= self.max_rounds:
                return self.response(context, "failed", "Agent reached the execution round limit.")
            context.rounds += 1
            if self.workflows:
                self.workflows.save(context, "planning")
            try:
                reply = await self.planner.plan(context.history)
            except LLMProviderError as exc:
                return self.response(context, "failed", str(exc))
            except Exception:
                return self.response(
                    context,
                    "failed",
                    "LLM request failed. Check the selected provider, model, and connectivity. "
                    "Use /doctor or the dashboard diagnostics for configuration details.",
                )
            if context.cancel_requested:
                return self._cancelled(context)
            context.history.extend(reply.output)
            if len(reply.calls) > 32:
                return self.response(context, "failed", "Requested plan exceeds 32 actions.")
            if not reply.calls:
                user = next(x for x in reversed(context.history) if x.get("role") == "user")
                summary = (
                    self.planner.privacy.summary(context.steps) if context.steps else reply.text
                )
                self.history = [
                    *self.history[-18:],
                    user,
                    {"role": "assistant", "content": summary},
                ]
                return self.response(context, "completed", reply.text or "Request completed.")
            try:
                context.queue = self._prepare(reply.calls)
            except ValueError:
                return self.response(
                    context, "failed", "Requested plan contains invalid or blocked tool arguments."
                )
            if self.workflows:
                self.workflows.save(context, "running")

    def list_workflows(self) -> list[dict]:
        return self.workflows.list() if self.workflows else []

    def workflow_page(self, query: WorkflowQuery) -> dict:
        if not self.workflows:
            raise ValueError("Workflow persistence is not configured.")
        return self.workflows.page(query)

    async def preview_workflow_cleanup(self, policy: RetentionPolicy) -> dict:
        async with self.lock:
            if not self.workflows:
                raise ValueError("Workflow persistence is not configured.")
            context = AgentContext(
                recovering=True, completion_message="Workflow cleanup completed."
            )
            try:
                preview = self.workflows.preview_prune(policy)
                count = len(preview["records"])
                if not count:
                    return self.response(
                        context, "completed", "No old workflow records match this policy."
                    )
                arguments = PruneInput(
                    cutoff_utc=preview["cutoff_utc"],
                    keep_recent=preview["keep_recent"],
                    records=preview["records"],
                )
                context.confirmation_message = (
                    f"Permanently remove these {count} workflow records from the journal? "
                    f"{preview['total_eligible']} records match; "
                    "at most 200 are removed per approval. "
                    "Project files, aliases, and unfinished workflows are preserved."
                )
                context.queue = self._prepare(
                    [
                        ToolCall(
                            call_id=uuid4().hex,
                            name="prune_workflow_records",
                            arguments=arguments.model_dump(),
                        )
                    ],
                    allow_private=True,
                )
                return await self._advance(context)
            except (sqlite3.Error, OSError):
                return self._storage_failure(context)

    async def request_tool(self, name: str, arguments: dict) -> dict:
        """Trusted service entry point. HTTP routes choose a fixed tool, never client code."""
        async with self.lock:
            context = AgentContext(recovering=True, completion_message="Action completed.")
            context.queue = self._prepare(
                [ToolCall(call_id=uuid4().hex, name=name, arguments=arguments)]
            )
            try:
                return await self._advance(context)
            except (sqlite3.Error, OSError):
                return self._storage_failure(context)

    def get_preferences(self) -> dict[str, str]:
        if not self.preferences:
            raise ValueError("Preferences are not configured.")
        return self.preferences.get()

    def list_projects(self) -> list[dict[str, str]]:
        if not self.preferences:
            raise ValueError("Preferences are not configured.")
        return [
            {"name": item.key.removeprefix("project:"), "path": item.value}
            for item in self.preferences.memory.projects()
        ]

    def get_workflow(self, request_id: str) -> dict:
        if not self.workflows:
            raise ValueError("Workflow persistence is not configured.")
        return self.workflows.get(request_id)

    async def resume_workflow(self, request_id: str) -> dict:
        async with self.lock:
            if self.tasks.get(request_id, {}).get("status") in {"queued", "running"}:
                raise ValueError("This workflow already has an active task. Follow that task.")
            saved = self.get_workflow(request_id)
            if not saved.get("recoverable", False):
                raise ValueError(
                    "This plan includes arguments excluded from persistent storage. "
                    "Use its current approval token or send a new request after restart."
                )
            if saved["status"] not in {"paused", "confirmation_required"}:
                raise ValueError(
                    "Workflow cannot be resumed. Interrupted actions have an uncertain "
                    "outcome; inspect the Mac and send a new request instead."
                )
            context = AgentContext(
                request_id=request_id, steps=saved["steps"], rounds=saved["rounds"], recovering=True
            )
            context.queue = self._prepare([ToolCall.model_validate(c) for c in saved["queue"]])
            self.confirmations.invalidate(request_id)
            try:
                return await self._advance(context)
            except (sqlite3.Error, OSError):
                return self._storage_failure(context)

    async def cancel_workflow(self, request_id: str) -> dict:
        async with self.lock:
            if self.tasks.get(request_id, {}).get("status") in {"queued", "running"}:
                raise ValueError("This workflow has an active task. Stop that task instead.")
            saved = self.get_workflow(request_id)
            if saved["status"] not in {"paused", "confirmation_required", "interrupted"}:
                raise ValueError("Only paused, pending, or interrupted workflows can be cancelled.")
            self.confirmations.invalidate(request_id)
            context = AgentContext(request_id=request_id, steps=saved["steps"])
            return self.response(
                context,
                "cancelled",
                "Remaining steps cancelled. Completed or uncertain OS actions were not undone.",
            )

    def reset_conversation(self) -> None:
        self.history.clear()

    async def close(self) -> None:
        if self.background and not self.background.done():
            for state in self.tasks.values():
                if state["status"] in {"queued", "running"}:
                    state["context"].cancel_requested = True
            await self.background
        # Keep the database lock until active execution (including cancellation cleanup) ends.
        async with self.lock:
            try:
                if hasattr(self.planner.llm, "close"):
                    await self.planner.llm.close()
            finally:
                if self.workflows:
                    self.workflows.close()
