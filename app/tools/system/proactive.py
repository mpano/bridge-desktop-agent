"""Tools for the proactive assistant: watches and heads-up settings."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from app.assistant.watchers import GITHUB_MINE, JIRA_MINE, JIRA_WHEN, PR_WHEN, WatchError
from app.assistant.watchers import describe as describe_target
from app.integrations.work import ISSUE_KEY
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.workflows.schedules import ScheduleStore
from app.workflows.watches import ProactiveStore

WATCH_KINDS = (
    "email",
    "slack",
    "github_pr",
    "github_release",
    "jira",
    "app",
    "github_mine",
    "jira_mine",
)


class WatchInput(Input):
    kind: Literal[
        "email", "slack", "github_pr", "github_release", "jira", "app", "github_mine", "jira_mine"
    ]
    label: str = Field(
        min_length=1, max_length=80, description="Short name, e.g. 'PR 412 merged', 'Sarah replies'"
    )
    query: str = Field(
        default="",
        max_length=200,
        pattern=r"^[^\x00-\x1f\x7f]*$",
        description="email/slack: Gmail search (e.g. 'from:olivier', 'subject:invoice') or Slack "
        "search; 'mentions' watches Slack mentions of the user",
    )
    once: bool | None = Field(
        default=None,
        description="Stop after the first time (e.g. 'when Sarah replies'). Default: true for "
        "GitHub/Jira/app conditions, false for email/slack.",
    )
    repo: str = Field(
        default="", max_length=140, description="GitHub owner/name (optional for PRs)"
    )
    number: int | None = Field(default=None, ge=1, description="Pull request number")
    key: str = Field(default="", max_length=40, description="Jira issue key, e.g. ABC-9")
    when: str = Field(
        default="",
        max_length=30,
        description="github_pr: merged, closed, approved, changes_requested, checks_pass, "
        "checks_fail, checks_done, review, comment, any. jira: status_to (with status), done, "
        "status, comment, assignee, any.",
    )
    status: str = Field(default="", max_length=60, description="Jira status for status_to, e.g. QA")
    app: str = Field(default="", max_length=80, description="Connected app name (kind=app)")
    tool: str = Field(default="", max_length=120, description="A read-only tool of that app")
    arguments: dict[str, Any] = Field(default_factory=dict, description="The tool's arguments")
    condition: str = Field(
        default="", max_length=300, description="kind=app: what to wait for, in plain words"
    )
    days: int | None = Field(default=None, ge=1, le=90, description="Give up after this many days")
    event: str = Field(
        default="",
        max_length=30,
        description="github_mine (any of yours): merged, review_requested, assigned, opened. "
        "jira_mine: assigned, done.",
    )

    @model_validator(mode="after")
    def complete(self):
        if self.kind in {"email", "slack"}:
            if not self.query.strip():
                raise ValueError("Say what to look for, e.g. from:olivier.")
        elif self.kind == "github_pr":
            if not self.number:
                raise ValueError("Which pull request? Give its number.")
            self.when = self.when or "any"
            if self.when not in PR_WHEN:
                raise ValueError(f"For a pull request, 'when' is one of: {', '.join(PR_WHEN)}.")
        elif self.kind == "github_release":
            if not self.repo.strip():
                raise ValueError("Which repository (owner/name)?")
        elif self.kind == "jira":
            if not ISSUE_KEY.fullmatch(self.key.strip().upper()):
                raise ValueError("Use a Jira issue key like ABC-9.")
            self.when = self.when or ("status_to" if self.status.strip() else "any")
            if self.when not in JIRA_WHEN:
                raise ValueError(f"For Jira, 'when' is one of: {', '.join(JIRA_WHEN)}.")
            if self.when == "status_to" and not self.status.strip():
                raise ValueError("Which status should it move to?")
        elif self.kind == "github_mine":
            if self.event not in GITHUB_MINE:
                raise ValueError(f"For your GitHub, 'event' is one of: {', '.join(GITHUB_MINE)}.")
        elif self.kind == "jira_mine":
            if self.event not in JIRA_MINE:
                raise ValueError(f"For your Jira, 'event' is one of: {', '.join(JIRA_MINE)}.")
        elif not (self.app.strip() and self.tool.strip()):
            raise ValueError("Say which app and which of its tools to check (see apps_find).")
        if self.once is None:
            self.once = self.kind not in {
                "email",
                "slack",
                "github_release",
                "github_mine",
                "jira_mine",
            } and self.when not in {
                "any",
                "comment",
                "review",
                "status",
                "assignee",
            }
        return self

    def target(self) -> dict:
        if self.kind == "github_pr":
            return {"repo": self.repo.strip(), "number": self.number, "when": self.when}
        if self.kind == "github_release":
            return {"repo": self.repo.strip()}
        if self.kind == "jira":
            found = {"key": self.key.strip().upper(), "when": self.when}
            if self.when == "status_to":
                found["status"] = self.status.strip()
            return found
        if self.kind in {"github_mine", "jira_mine"}:
            return {"event": self.event}
        if self.kind == "app":
            return {
                "app": self.app.strip(),
                "tool": self.tool.strip(),
                "arguments": self.arguments,
                "condition": self.condition.strip(),
            }
        return {}


class WatchWords(Input):
    text: str = Field(min_length=3, max_length=300, pattern=r"^[^\x00-\x1f\x7f]+$")


class WatchDeleteInput(Input):
    about: str = Field(min_length=1, max_length=200, description="Words from the watch")


class SettingsInput(Input):
    meeting_prep: bool | None = None
    lead_minutes: int | None = Field(default=None, ge=1, le=60)
    evening_summary: bool | None = None
    evening_time: str | None = Field(default=None, description="24-hour HH:MM")
    morning_plan: bool | None = Field(default=None, description="Weekday 'Plan my day'")
    morning_time: str | None = Field(default=None, description="24-hour HH:MM")
    work_start: str | None = Field(default=None, description="Workday start, 24-hour HH:MM")
    work_end: str | None = Field(default=None, description="Workday end, 24-hour HH:MM")
    weekly_report: bool | None = Field(default=None, description="Friday weekly report draft")
    weekly_report_time: str | None = Field(default=None, description="Friday, 24-hour HH:MM")

    @field_validator("evening_time", "morning_time", "work_start", "work_end", "weekly_report_time")
    @classmethod
    def clock(cls, value):
        if value is None:
            return value
        hours, _, minutes = value.partition(":")
        if not (hours.isdigit() and minutes.isdigit() and int(hours) < 24 and int(minutes) < 60):
            raise ValueError("Use a 24-hour HH:MM time.")
        return f"{int(hours):02d}:{int(minutes):02d}"


class ProactiveController:
    def __init__(
        self,
        store: ProactiveStore,
        schedules: ScheduleStore,
        accounts=None,
        clock=lambda: datetime.now().astimezone(),
    ):
        self.store, self.schedules, self.accounts, self.clock = store, schedules, accounts, clock
        self.watchers = None  # app/assistant/watchers.py, set by bootstrap.
        self.proactive = None  # Runs the first check of a new watcher.

    async def _require(self, provider: str, name: str) -> None:
        if self.accounts is None:
            raise ValueError(f"Connect {name} in the dashboard's Connections page first.")
        try:
            accounts = (await self.accounts.list_accounts())["accounts"]
        except Exception:
            raise ValueError(f"Connect {name} in the dashboard's Connections page first.") from None
        if not any(item["provider"] == provider for item in accounts):
            raise ValueError(f"Connect {name} in the dashboard's Connections page first.")

    async def watch(self, args):
        if args.kind in {"email", "slack"}:
            await self._require(
                "gmail" if args.kind == "email" else "slack",
                "Gmail" if args.kind == "email" else "Slack",
            )
            watch = self.store.add_watch(
                args.kind, args.query.strip(), args.label.strip(), once=args.once, days=args.days
            )
            return self._view(watch)
        if self.watchers is None or self.proactive is None:
            raise ValueError("Watchers aren't available.")
        try:
            target = await self.watchers.prepare(args.kind, args.target())
        except WatchError as exc:
            raise ValueError(str(exc)) from None
        except Exception as exc:  # The service couldn't be reached, …
            raise ValueError(str(exc)[:200] or "Couldn't check that right now.") from None
        watch = self.store.add_watch(
            args.kind,
            describe_target(args.kind, target),
            args.label.strip(),
            target=target,
            once=args.once,
            days=args.days,
        )
        # Look right away: it may already have happened, or the target may not be readable.
        if not watch.baseline_done:
            await self.proactive.check_condition(watch, quiet=True)
            watch = self.store.watch(watch.id)
        if watch.last_error and not watch.baseline_done:
            self.store.remove_watch(watch.id)
            raise ValueError(watch.last_error)
        return self._view(watch)

    @staticmethod
    def _view(w) -> dict:
        return {
            "id": w.id,
            "kind": w.kind,
            "query": w.query,
            "label": w.label,
            "once": w.once,
            "status": w.status,
            "fired": w.fired,
            "fired_at": w.fired_at,
            "expires_at": w.expires_at,
            "last_checked": w.last_checked,
            "last_error": w.last_error,
            "url": w.state.get("url", "") if isinstance(w.state, dict) else "",
        }

    async def list(self, _):
        return {
            "watches": [self._view(w) for w in self.store.watches(everything=True)],
            "settings": self.store.settings(),
        }

    async def unwatch(self, args):
        words = args.about.casefold().split()
        matches = [
            w
            for w in self.store.watches()
            if all(word in f"{w.label} {w.query}".casefold() for word in words)
        ]
        if not matches:
            raise ValueError(f"You're not watching anything about “{args.about}”.")
        if len(matches) > 1:
            names = "; ".join(w.label for w in matches[:5])
            raise ValueError(f"Several watches match: {names}. Be more specific.")
        self.store.remove_watch(matches[0].id)
        return {"label": matches[0].label}

    def apply(self, **changes) -> dict:
        """Update settings. The morning brief and evening wrap-up run on their own (see
        app/assistant/briefs.py); older versions kept a scheduled request for each, which
        is removed here."""
        proposed = {**self.store.settings(), **changes}
        if proposed["work_end"] <= proposed["work_start"]:
            raise ValueError("The workday must end after it starts.")
        settings = self.store.update_settings(**changes)
        ids = {}
        for prefix in ("evening", "morning"):
            if settings[f"{prefix}_schedule_id"]:
                self.schedules.delete(settings[f"{prefix}_schedule_id"])
                ids[f"{prefix}_schedule_id"] = 0
        return self.store.update_settings(**ids) if ids else settings

    def upgrade_routines(self) -> None:
        """Once: move to the new brief and wrap-up, and turn the wrap-up on."""
        settings = self.store.settings()
        if settings.get("briefs_v1"):
            return
        self.apply(evening_summary=True)
        self.store.update_settings(briefs_v1=True)

    async def configure(self, args):
        changes = {key: value for key, value in args.model_dump().items() if value is not None}
        return {"settings": self.apply(**changes)}


def describe(settings: dict) -> str:
    meeting = (
        f"Meeting heads-up {settings['lead_minutes']} min before"
        if settings["meeting_prep"]
        else "Meeting heads-ups off"
    )
    evening = (
        f"evening summary weekdays at {settings['evening_time']}"
        if settings["evening_summary"]
        else "evening summary off"
    )
    morning = (
        f"plan my day weekdays at {settings['morning_time']}"
        if settings["morning_plan"]
        else "morning plan off"
    )
    hours = f"workday {settings['work_start']}–{settings['work_end']}"
    return f"{meeting} · {evening} · {morning} · {hours}"


def render_list(data: dict) -> str:
    lines = [describe(data["settings"])]
    if data["watches"]:
        lines.append("Watching:")
        for watch in data["watches"]:
            problem = f" ⚠ {watch['last_error']}" if watch["last_error"] else ""
            if watch["status"] == "done":
                lines.append(f"• ✓ {watch['label']} — {watch['fired']}")
            elif watch["status"] == "expired":
                lines.append(f"• {watch['label']} — stopped, it didn't happen in time")
            else:
                where = f"{watch['kind']}: " if watch["kind"] in {"email", "slack"} else ""
                lines.append(f"• {watch['label']} ({where}{watch['query']}){problem}")
    else:
        lines.append("Not watching anything. Try “tell me when PR 412 is merged”.")
    return "\n".join(lines)


def render_created(data: dict) -> str:
    if data["status"] == "done":
        return f"It already happened: {data['fired']}. No need to watch it."
    how = "(once, then I'll stop watching)" if data["once"] else "(every time)"
    every = "5 minutes" if data["kind"] == "app" else "2 minutes"
    return (
        f"✓ I'll let you know: {data['label']} {how}. Checked every {every} while Bridge is open."
    )


def register(registry, controller: ProactiveController):
    registry.register(
        Tool(
            "watch_create",
            "Tell the user when something happens ('tell me when …'). kind=email/slack: new "
            "matching messages ('when Olivier emails me': query='from:olivier'; 'when Sarah "
            "replies': query='from:sarah', once=true; Slack mentions: query='mentions'). "
            "kind=github_pr: a pull request is merged/approved/checks pass/fails/new comment "
            "(number; repo optional). kind=github_release: a new release of a repo. kind=jira: "
            "an issue moves to a status, is done, gets a comment (key, when, status). kind=app: "
            "a condition in a connected app, using one of its read-only tools found with "
            "apps_find (app, tool, arguments, condition). Bridge checks every few minutes and "
            "notifies the Mac and the phone.",
            WatchInput,
            RiskLevel.SAFE,
            controller.watch,
            render=render_created,
        )
    )
    registry.register(
        Tool(
            "watch_list",
            "Show what Bridge is watching and the meeting / evening summary settings.",
            Input,
            RiskLevel.SAFE,
            controller.list,
            render=render_list,
        )
    )
    registry.register(
        Tool(
            "watch_delete",
            "Stop one watch, found by words from it, e.g. 'stop watching Olivier'.",
            WatchDeleteInput,
            RiskLevel.SAFE,
            controller.unwatch,
            render=lambda data: f"✓ Stopped: {data['label']}",
        )
    )
    registry.register(
        Tool(
            "proactive_settings",
            "Turn meeting heads-ups on/off or change how many minutes before; turn the weekday "
            "evening summary or the weekday morning 'plan my day' on/off or change their "
            "times; set working hours used for planning.",
            SettingsInput,
            RiskLevel.SAFE,
            controller.configure,
            render=lambda data: "✓ " + describe(data["settings"]),
        )
    )
