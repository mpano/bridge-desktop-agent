"""Human-requested journal maintenance, never automatic or model-discoverable."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import Field, field_validator, model_validator

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from app.workflows.store import WorkflowRepository

TERMINAL_STATES = ("completed", "failed", "cancelled")
WorkflowFilter = Literal[
    "unfinished",
    "running",
    "planning",
    "confirmation_required",
    "paused",
    "interrupted",
    "completed",
    "failed",
    "cancelled",
]


class WorkflowQuery(Input):
    status: WorkflowFilter | None = None
    offset: int = Field(default=0, ge=0, le=1000000)
    limit: int = Field(default=100, ge=1, le=100)


class RetentionPolicy(Input):
    older_than_days: int = Field(default=30, ge=1, le=3650)
    keep_recent: int = Field(default=100, ge=0, le=10000)


class RetentionRecord(Input):
    request_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    status: Literal["completed", "failed", "cancelled"]
    updated_at: str


class PruneInput(Input):
    cutoff_utc: str
    keep_recent: int = Field(ge=0, le=10000)
    records: list[RetentionRecord] = Field(min_length=1, max_length=200)

    @field_validator("cutoff_utc")
    @classmethod
    def cutoff(cls, value: str) -> str:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        if parsed >= datetime.now(UTC):
            raise ValueError("Cutoff must be in the past.")
        return value

    @model_validator(mode="after")
    def unique_records(self):
        if len({record.request_id for record in self.records}) != len(self.records):
            raise ValueError("Duplicate workflow records are not allowed.")
        return self


def register(registry: ToolRegistry, repository: "WorkflowRepository") -> None:
    async def prune(arguments: PruneInput) -> dict:
        return repository.prune(arguments)

    registry.register(
        Tool(
            "prune_workflow_records",
            "Remove only the reviewed terminal workflow records from the local journal.",
            PruneInput,
            RiskLevel.CONFIRM,
            prune,
            persist_arguments=False,
            expose_to_llm=False,
        )
    )
