"""Recipes as tools: "when my PR merges, post it in #releases", "what recipes do I have?"."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.security.risk import RiskLevel
from app.tools.base import Input, Tool


class RecipeInput(Input):
    text: str = Field(
        min_length=5,
        max_length=500,
        description="The user's request in their words, e.g. 'when a PR of mine is merged, "
        "post its title and link in #releases'",
    )
    plan: dict[str, Any] | None = Field(
        default=None, description="Leave empty; Bridge fills it in for the user to approve."
    )


class RecipeDeleteInput(Input):
    about: str = Field(min_length=1, max_length=100, description="Words from the recipe's name")


def render_list(data: dict) -> str:
    if not data["recipes"]:
        return "No recipes yet. Try “when a PR of mine is merged, post it in #releases”."
    lines = []
    for recipe in data["recipes"]:
        mode = "automatic" if recipe["mode"] == "auto" else "asks first"
        paused = " · paused" if not recipe["enabled"] else ""
        lines.append(
            f"• {recipe['name']}: when {recipe['when']} → {'; '.join(recipe['steps'])} "
            f"({mode}{paused})"
        )
    if data["waiting"]:
        lines.append(f"{len(data['waiting'])} waiting for your OK on Today.")
    return "\n".join(lines)


def register(registry, recipes) -> None:
    async def resolve(args):
        """Turn the words into the exact recipe BEFORE asking, so the user approves it."""
        if args.plan:
            plan = recipes.check(args.plan)
        else:
            plan = recipes.check(await recipes.understand(args.text))
        return args.model_copy(update={"plan": plan.model_dump(exclude_defaults=True)})

    async def create(args):
        made = await recipes.create(args.plan)
        return made

    async def listing(_):
        return recipes.overview()

    async def delete(args):
        words = args.about.casefold().split()
        found = [
            r
            for r in recipes.overview()["recipes"]
            if all(w in r["name"].casefold() for w in words)
        ]
        if not found:
            raise ValueError(f"No recipe matches “{args.about}”.")
        if len(found) > 1:
            raise ValueError("Several recipes match: " + "; ".join(r["name"] for r in found[:5]))
        return recipes.delete(found[0]["id"])

    registry.register(
        Tool(
            "recipe_create",
            "Create an automation: when something happens (a PR of the user's merges, a review "
            "is requested, a Jira ticket is assigned or done, a release ships, an email or "
            "Slack message arrives), do something (post in Slack, send an email, comment on or "
            "move a Jira ticket, comment on GitHub, add a reminder, or ask Bridge to do a "
            "task). Pass the user's words as text. Each run asks the user first unless they "
            "switch the recipe to automatic in Automations.",
            RecipeInput,
            RiskLevel.CONFIRM,
            create,
            resolve=resolve,
            confirmation_message="Create this recipe? Each run will ask you first.",
            render=lambda d: (
                f"✓ Recipe “{d['name']}”: when {d['when']} → "
                f"{'; '.join(d['steps'])}. Each run asks you first; switch it to automatic in "
                "Automations if you trust it."
            ),
        )
    )
    registry.register(
        Tool(
            "recipe_list",
            "List the user's recipes (automations) and runs waiting for their OK.",
            Input,
            RiskLevel.SAFE,
            listing,
            render=render_list,
        )
    )
    registry.register(
        Tool(
            "recipe_delete",
            "Delete one recipe, found by words from its name.",
            RecipeDeleteInput,
            RiskLevel.SAFE,
            delete,
            render=lambda d: f"✓ Removed the recipe “{d['name']}”.",
        )
    )
