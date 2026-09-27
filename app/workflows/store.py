"""A conservative checkpoint journal, not an exactly-once OS transaction system."""

import fcntl
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from app.agent.context import AgentContext
from app.llm.models import ToolCall
from app.workflows.retention import PruneInput, RetentionPolicy, WorkflowQuery


class WorkflowRepository(Protocol):
    def save(
        self,
        context: AgentContext,
        status: str,
        *,
        in_flight: bool = False,
        queue: list[ToolCall] | None = None,
    ) -> None: ...
    def list(self) -> list[dict]: ...
    def page(self, query: WorkflowQuery) -> dict: ...
    def preview_prune(self, policy: RetentionPolicy) -> dict: ...
    def prune(self, arguments: PruneInput) -> dict: ...
    def get(self, request_id: str) -> dict: ...
    def close(self) -> None: ...


class SQLiteWorkflows:
    """One process per database; never replay a possibly executed operation."""

    def __init__(self, path: Path, can_persist: Callable[[ToolCall], bool] | None = None):
        self.path = path.expanduser().resolve()
        self.can_persist = can_persist or (lambda call: False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._lock = open(lock_path, "a")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock.close()
            raise RuntimeError(
                "Another Bridge process is using this database. Close it first."
            ) from exc
        try:
            with sqlite3.connect(self.path) as db:
                db.execute("""CREATE TABLE IF NOT EXISTS workflows (
                    request_id TEXT PRIMARY KEY, status TEXT NOT NULL, snapshot TEXT NOT NULL,
                    in_flight INTEGER NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )""")
                columns = {column[1] for column in db.execute("PRAGMA table_info(workflows)")}
                if "revision" not in columns:
                    db.execute(
                        "ALTER TABLE workflows ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
                    )
                db.execute("CREATE INDEX IF NOT EXISTS workflows_updated ON workflows(updated_at)")
                rows = db.execute(
                    "SELECT request_id, snapshot, in_flight FROM workflows "
                    "WHERE status IN ('running', 'planning', 'confirmation_required')"
                ).fetchall()
                for request_id, snapshot, in_flight in rows:
                    state = json.loads(snapshot)
                    recoverable = state.get("recoverable", False)
                    status = (
                        "interrupted"
                        if in_flight or not state["queue"] or not recoverable
                        else "paused"
                    )
                    db.execute(
                        "UPDATE workflows SET status=?, revision=revision+1 WHERE request_id=?",
                        (status, request_id),
                    )
            os.chmod(self.path, 0o600)
        except BaseException:
            self.close()
            raise

    def save(
        self,
        context: AgentContext,
        status: str,
        *,
        in_flight: bool = False,
        queue: list[ToolCall] | None = None,
    ) -> None:
        remaining = context.queue if queue is None else queue
        if status in {"completed", "failed", "cancelled"}:
            remaining = []
        # Deliberately omit prompts, LLM responses, tool output, and approval tokens.
        recoverable = all(self.can_persist(call) for call in remaining)
        snapshot = {
            "recoverable": recoverable,
            "queue": [
                call.model_dump()
                if self.can_persist(call)
                else {
                    "name": call.name,
                    "call_id": call.call_id,
                    "arguments": {},
                    "arguments_omitted": True,
                }
                for call in remaining
            ],
            "steps": [
                {key: step[key] for key in ("tool", "call_id", "success", "status")}
                for step in context.steps
            ],
            "rounds": context.rounds,
        }
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO workflows (request_id,status,snapshot,in_flight) VALUES (?,?,?,?) "
                "ON CONFLICT(request_id) DO UPDATE SET status=excluded.status, "
                "snapshot=excluded.snapshot,in_flight=excluded.in_flight,"
                "updated_at=CURRENT_TIMESTAMP,revision=workflows.revision+1",
                (context.request_id, status, json.dumps(snapshot), int(in_flight)),
            )

    def list(self) -> list[dict]:
        return self.page(WorkflowQuery())["workflows"]

    def page(self, query: WorkflowQuery) -> dict:
        where, parameters = "", []
        if query.status == "unfinished":
            where = "WHERE status NOT IN ('completed','failed','cancelled') OR in_flight=1"
        elif query.status:
            where, parameters = "WHERE status=?", [query.status]
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN")
            total = db.execute(f"SELECT COUNT(*) FROM workflows {where}", parameters).fetchone()[0]
            rows = db.execute(
                f"SELECT request_id,status,updated_at,in_flight FROM workflows {where} "
                "ORDER BY updated_at DESC, rowid DESC LIMIT ? OFFSET ?",
                [*parameters, query.limit, query.offset],
            ).fetchall()
        items = [
            dict(zip(("request_id", "status", "updated_at", "in_flight"), row, strict=True))
            for row in rows
        ]
        return {
            "workflows": items,
            "total": total,
            "offset": query.offset,
            "limit": query.limit,
            "has_more": query.offset + len(items) < total,
        }

    @staticmethod
    def _eligible_sql() -> str:
        return (
            "status IN ('completed','failed','cancelled') AND in_flight=0 "
            "AND updated_at < ? AND request_id NOT IN ("
            "SELECT request_id FROM workflows WHERE status IN ('completed','failed','cancelled') "
            "ORDER BY updated_at DESC,rowid DESC LIMIT ?)"
        )

    def preview_prune(self, policy: RetentionPolicy) -> dict:
        cutoff = (datetime.now(UTC) - timedelta(days=policy.older_than_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN")
            parameters = (cutoff, policy.keep_recent)
            where = self._eligible_sql()
            total = db.execute(
                f"SELECT COUNT(*) FROM workflows WHERE {where}", parameters
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT request_id,revision,status,updated_at FROM workflows WHERE {where} "
                "ORDER BY updated_at ASC,rowid ASC LIMIT 200",
                parameters,
            ).fetchall()
        records = [
            dict(zip(("request_id", "revision", "status", "updated_at"), row, strict=True))
            for row in rows
        ]
        return {
            "cutoff_utc": cutoff,
            "keep_recent": policy.keep_recent,
            "records": records,
            "total_eligible": total,
            "has_more": total > len(records),
        }

    def prune(self, arguments: PruneInput) -> dict:
        # Recheck the complete approved set in one write transaction. Never substitute
        # newly eligible records if any original candidate changed or disappeared.
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            where = self._eligible_sql()
            for record in arguments.records:
                row = db.execute(
                    "SELECT revision,status,updated_at FROM workflows "
                    f"WHERE {where} AND request_id=?",
                    (arguments.cutoff_utc, arguments.keep_recent, record.request_id),
                ).fetchone()
                if row != (record.revision, record.status, record.updated_at):
                    raise ValueError(
                        "Workflow records changed since preview. Nothing was removed; "
                        "request a new cleanup preview."
                    )
            db.executemany(
                "DELETE FROM workflows WHERE request_id=?",
                [(record.request_id,) for record in arguments.records],
            )
        return {
            "deleted_count": len(arguments.records),
            "deleted_request_ids": [record.request_id for record in arguments.records],
            "message": "Removed the approved workflow records. "
            "Project files and aliases were not changed.",
        }

    def get(self, request_id: str) -> dict:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT status,snapshot,in_flight FROM workflows WHERE request_id=?", (request_id,)
            ).fetchone()
        if row is None:
            raise ValueError("Unknown workflow.")
        return {
            "request_id": request_id,
            "status": row[0],
            **json.loads(row[1]),
            "in_flight": bool(row[2]),
        }

    def close(self) -> None:
        if not self._lock.closed:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
