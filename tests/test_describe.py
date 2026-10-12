"""One box for every automation: which kind did you describe?"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.assistant.describe import kind_of


@pytest.mark.parametrize(
    "text,kind",
    [
        ("tell me when PR 412 is merged", "watch"),
        ("Let me know when Sarah replies", "watch"),
        ("when ABC-9 moves to QA", "watch"),
        ("When a PR of mine is merged, post “🚀 {title}” in #releases", "recipe"),
        ("whenever someone asks me for a review, add a reminder", "recipe"),
        ("every Friday at 5, tell me what slipped this week", "schedule"),
        ("each morning brief me on my calendar", "schedule"),
        ("on mondays summarize my open tickets", "schedule"),
    ],
)
async def test_clear_wording_needs_no_model(text, kind):
    llm = SimpleNamespace(complete=AsyncMock())
    assert await kind_of(text, llm) == kind
    llm.complete.assert_not_awaited()


async def test_unclear_wording_asks_the_model():
    llm = SimpleNamespace(complete=AsyncMock(return_value="recipe"))
    assert await kind_of("new Jira bugs should go to #triage", llm) == "recipe"
    llm.complete.return_value = "I'm not sure"
    assert await kind_of("something vague", llm) == "schedule"
    assert await kind_of("something vague", None) == "schedule"
