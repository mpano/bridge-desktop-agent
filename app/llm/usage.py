"""What Bridge spends on AI, and which model does which job.

Every model call says which task it's for (Ask, sorting the inbox, promises, drafts…). Tasks
use the Smart model or the Fast model you choose; Bridge records the tokens each call used and
what they cost at OpenAI's prices, and keeps you inside a monthly budget if you set one.
Prices are OpenAI's list prices (standard tier, short context) from October 2026; actual bills
can differ slightly (e.g. discounts or long-context rates).
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

# $ per 1M tokens: input, cached input, output.
PRICES = {
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5.2": (1.75, 0.175, 14.00),
    "gpt-5.1": (1.25, 0.125, 10.00),
    "gpt-5": (1.25, 0.125, 10.00),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
    "gpt-4o": (2.50, 1.25, 10.00),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "o3": (2.00, 0.50, 8.00),
    "o4-mini": (1.10, 0.275, 4.40),
    # Realtime voice: audio tokens (text tokens are cheaper; this errs on the high side).
    "gpt-realtime-2.1": (32.00, 0.40, 64.00),
    "gpt-realtime-mini": (10.00, 0.30, 20.00),
}
# Models offered for the Smart and Fast choices (text models that can use tools).
CHOICES = [
    "gpt-6.1-sol",
    "gpt-6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.4-mini",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-4o-mini",
]
TASKS = {
    # task: (what you'd call it, tier)
    "ask": ("Ask and voice commands", "smart"),
    "plan": ("Planning your day", "smart"),
    "drafts": ("Reply drafts", "smart"),
    "screen": ("Reading your screen", "smart"),
    "learning": ("Learning from your corrections", "smart"),
    "report": ("Weekly report", "smart"),
    "inbox": ("Sorting email", "fast"),
    "slack": ("Sorting Slack", "fast"),
    "promises": ("Finding promises", "fast"),
    "brief": ("Morning brief", "fast"),
    "watchers": ("Watchers", "fast"),
    "text": ("Dictation and text actions", "fast"),
    "talk": ("Conversations (voice)", "voice"),
    "operator": ("Using your Mac", "operator"),
}
BACKGROUND = {
    "promises",
    "inbox",
    "slack",
    "brief",
    "learning",
    "watchers",
}  # Paused when saving money.

_task: ContextVar[str] = ContextVar("bridge_llm_task", default="ask")


@contextmanager
def task(name: str):
    """Label the model calls made inside this block (for routing and the Usage page)."""
    token = _task.set(name)
    try:
        yield
    finally:
        _task.reset(token)


def labelled(name: str):
    """Decorator: every model call inside this async function counts as `name`."""

    def wrap(function):
        import functools

        @functools.wraps(function)
        async def run(*args, **kwargs):
            with task(name):
                return await function(*args, **kwargs)

        return run

    return wrap


def current_task() -> str:
    return _task.get()


def price_of(model: str) -> tuple[float, float, float] | None:
    if model in PRICES:
        return PRICES[model]
    # Dated snapshots (gpt-4.1-mini-2025-04-14) cost the same as their model.
    for name in sorted(PRICES, key=len, reverse=True):
        if model.startswith(name + "-"):
            return PRICES[name]
    return None


def cost(model: str, input_tokens: int, cached: int, output: int) -> float | None:
    price = price_of(model)
    if price is None:
        return None
    fresh = max(0, input_tokens - cached)
    return (fresh * price[0] + cached * price[1] + output * price[2]) / 1_000_000


class Usage:
    def __init__(self, path: Path | str, settings, clock=time.time):
        self.path, self.settings, self.clock = str(path), settings, clock
        self.notify = None
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS llm_usage (
                    day TEXT NOT NULL, task TEXT NOT NULL, model TEXT NOT NULL,
                    calls INTEGER NOT NULL, input INTEGER NOT NULL, cached INTEGER NOT NULL,
                    output INTEGER NOT NULL, cost REAL NOT NULL, priced INTEGER NOT NULL,
                    PRIMARY KEY (day, task, model));
                CREATE TABLE IF NOT EXISTS llm_budget_alerts (month TEXT, level INTEGER,
                    PRIMARY KEY (month, level));
                """
            )

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    def _day(self) -> str:
        return datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")

    # Routing ------------------------------------------------------------------------------

    def model_for(self, name: str | None = None) -> str:
        """The model for this task: Smart or Fast, or the Fast one for everything while
        you're over budget in Save money mode."""
        name = name or current_task()
        tier = TASKS.get(name, ("", "smart"))[1]
        smart = getattr(self.settings, "model_smart", "") or self.settings.openai_model
        fast = getattr(self.settings, "model_fast", "") or self.settings.openai_model
        if self.saving():
            return fast
        return fast if tier == "fast" else smart

    def saving(self) -> bool:
        return getattr(self.settings, "budget_mode", "warn") == "save" and self.over_budget()

    def allow_background(self, name: str) -> bool:
        """Background checks pause while over budget in Save money mode."""
        return not (name in BACKGROUND and self.saving())

    # Recording ----------------------------------------------------------------------------

    def record(
        self,
        name: str,
        model: str,
        input_tokens: int = 0,
        cached: int = 0,
        output: int = 0,
        dollars: float | None = None,
    ) -> None:
        if dollars is None:
            dollars = cost(model, input_tokens, cached, output)
        priced = dollars is not None
        with self._db() as db:
            db.execute(
                "INSERT INTO llm_usage VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(day, task, model) DO UPDATE SET calls = calls + 1, "
                "input = input + excluded.input, cached = cached + excluded.cached, "
                "output = output + excluded.output, cost = cost + excluded.cost, "
                "priced = MIN(priced, excluded.priced)",
                (
                    self._day(),
                    name,
                    model,
                    int(input_tokens),
                    int(cached),
                    int(output),
                    float(dollars or 0.0),
                    int(priced),
                ),
            )

    def record_response(self, name: str, model: str, usage) -> None:
        """From an OpenAI Responses API `usage` (object or dict)."""
        if usage is None:
            return
        get = (
            usage.get
            if isinstance(usage, dict)
            else lambda key, default=None: getattr(usage, key, default)
        )
        details = get("input_tokens_details") or {}
        cached = (
            details.get("cached_tokens", 0)
            if isinstance(details, dict)
            else getattr(details, "cached_tokens", 0)
        )
        self.record(
            name,
            model,
            int(get("input_tokens", 0) or 0),
            int(cached or 0),
            int(get("output_tokens", 0) or 0),
        )

    # Reading ------------------------------------------------------------------------------

    def month_spend(self, month: str | None = None) -> float:
        month = month or self._day()[:7]
        with self._db() as db:
            row = db.execute(
                "SELECT SUM(cost) FROM llm_usage WHERE day LIKE ?", (f"{month}-%",)
            ).fetchone()
        return float(row[0] or 0.0)

    def over_budget(self) -> bool:
        budget = float(getattr(self.settings, "monthly_budget", 0) or 0)
        return bool(budget) and self.month_spend() >= budget

    def summary(self) -> dict:
        now = datetime.fromtimestamp(self.clock())
        month = now.strftime("%Y-%m")
        with self._db() as db:
            rows = db.execute(
                "SELECT day, task, model, calls, input, cached, output, cost, priced "
                "FROM llm_usage "
                "WHERE day LIKE ?",
                (f"{month}-%",),
            ).fetchall()
        by_task: dict[str, dict] = {}
        by_model: dict[str, dict] = {}
        by_day: dict[str, float] = {}
        unpriced = False
        for day, name, model, calls, inp, _cached, out, dollars, priced in rows:
            t = by_task.setdefault(
                name, {"task": name, "label": TASKS.get(name, (name,))[0], "calls": 0, "cost": 0.0}
            )
            t["calls"] += calls
            t["cost"] += dollars
            m = by_model.setdefault(model, {"model": model, "calls": 0, "tokens": 0, "cost": 0.0})
            m["calls"] += calls
            m["tokens"] += inp + out
            m["cost"] += dollars
            by_day[day] = by_day.get(day, 0.0) + dollars
            unpriced = unpriced or not priced
        spent = sum(by_day.values())
        days_passed = now.day
        days_in_month = (
            datetime(now.year + now.month // 12, now.month % 12 + 1, 1)
            - datetime(now.year, now.month, 1)
        ).days
        budget = float(getattr(self.settings, "monthly_budget", 0) or 0)
        return {
            "month": month,
            "spent": round(spent, 4),
            "projected": round(spent / max(days_passed, 1) * days_in_month, 2),
            "budget": budget,
            "budget_mode": getattr(self.settings, "budget_mode", "warn"),
            "saving": self.saving(),
            "tasks": sorted(by_task.values(), key=lambda t: -t["cost"]),
            "models": sorted(by_model.values(), key=lambda m: -m["cost"]),
            "days": [{"day": d, "cost": round(c, 4)} for d, c in sorted(by_day.items())],
            "unpriced": unpriced,
            "smart": getattr(self.settings, "model_smart", "") or self.settings.openai_model,
            "fast": getattr(self.settings, "model_fast", "") or self.settings.openai_model,
            "choices": [
                {"model": name, "input": PRICES[name][0], "output": PRICES[name][2]}
                for name in CHOICES
            ],
            "routing": [
                {"task": name, "label": label, "tier": tier}
                for name, (label, tier) in TASKS.items()
            ],
        }

    async def check_budget(self) -> None:
        """Called by the proactive loop: tell you once at 80% and once at 100% each month."""
        budget = float(getattr(self.settings, "monthly_budget", 0) or 0)
        if not budget or self.notify is None:
            return
        spent = self.month_spend()
        month = self._day()[:7]
        for level in (100, 80):
            if spent >= budget * level / 100:
                with self._db() as db:
                    fresh = db.execute(
                        "INSERT OR IGNORE INTO llm_budget_alerts VALUES (?, ?)", (month, level)
                    ).rowcount
                if fresh:
                    saving = getattr(self.settings, "budget_mode", "warn") == "save"
                    what = (
                        "Bridge now uses the fast model and pauses background checks."
                        if level == 100 and saving
                        else "You can change the budget in Settings › Usage."
                    )
                    await self.notify(
                        f"AI spending: {level}% of your ${budget:.0f} budget",
                        f"${spent:.2f} so far this month. {what}",
                        "settings",
                    )
                break
