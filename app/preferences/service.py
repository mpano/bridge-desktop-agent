from pydantic import field_validator

from app.memory.store import MemoryRepository
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool
from app.tools.registry import ToolRegistry
from app.tools.system.apps import AppInput, app_name


class ApplicationPreferencesInput(Input):
    default_browser: str
    default_editor: str

    @field_validator("default_browser", "default_editor")
    @classmethod
    def validate_app(cls, value: str) -> str:
        return AppInput(app_name=app_name(value.strip())).app_name


class ApplicationPreferences:
    """Public preferences only; deliberately has no access to provider credentials."""

    def __init__(self, memory: MemoryRepository, browser: str, editor: str):
        self.memory = memory
        self.defaults = {"default_browser": browser, "default_editor": editor}

    def get(self) -> dict[str, str]:
        return {
            key: stored.value if (stored := self.memory.get(key)) else default
            for key, default in self.defaults.items()
        }

    def browser(self) -> str:
        return self.get()["default_browser"]

    def editor(self) -> str:
        return self.get()["default_editor"]

    def update(self, values: ApplicationPreferencesInput) -> dict[str, str]:
        self.memory.set_app_preferences(values.model_dump())
        return self.get()


def register(registry: ToolRegistry, preferences: ApplicationPreferences) -> None:
    async def get(args: Input) -> dict:
        return preferences.get()

    async def set_values(args: ApplicationPreferencesInput) -> dict:
        return preferences.update(args)

    registry.register(
        Tool(
            "get_preferences",
            "Read the default browser and editor.",
            Input,
            RiskLevel.SAFE,
            get,
            persist_arguments=True,
        )
    )
    registry.register(
        Tool(
            "set_preferences",
            "Change default browser and editor after confirmation. "
            "Only do this when the user asks to change their preferences.",
            ApplicationPreferencesInput,
            RiskLevel.CONFIRM,
            set_values,
            persist_arguments=True,
        )
    )
