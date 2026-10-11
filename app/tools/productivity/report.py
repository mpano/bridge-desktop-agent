"""The weekly report as a tool: "write my weekly update", "what did I do this week?"."""

from __future__ import annotations

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class ReportInput(Input):
    fresh: bool = Field(default=False, description="Rewrite it from the latest data")


def render(data: dict) -> str:
    sent = f"\n\nSent to {data['sent'][-1]['to']}." if data["sent"] else ""
    return (
        f"Weekly report · {data['range']}\n\n{data['text']}{sent}\n\n"
        "Edit, copy or send it from the report card on Today."
    )


def register(registry, reports) -> None:
    async def write(args):
        return await reports.write(fresh=args.fresh)

    registry.register(
        Tool(
            "weekly_report",
            "Write (or show) the user's weekly status report from this week's merged and "
            "reviewed pull requests, Jira tickets and GitHub issues closed, work in progress, "
            "promises kept and what's coming up. Use for 'write my weekly update', 'what did I "
            "do this week', 'status report'. It's a draft; sending happens from Today or with "
            "the Slack/email tools after the user approves.",
            ReportInput,
            RiskLevel.SAFE,
            write,
            render=render,
        )
    )
