from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.security.risk import RiskLevel

if TYPE_CHECKING:
    from app.tools.base import Tool


class PolicyError(ValueError):
    pass


class SecurityPolicy:
    def evaluate(self, tool: "Tool", arguments: BaseModel) -> RiskLevel:
        risk = tool.classify(arguments)
        if risk == RiskLevel.DANGEROUS:
            raise PolicyError("Dangerous actions are blocked.")
        return risk


def checked_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    parts = {p.lower() for p in path.parts}
    forbidden = {
        ".ssh",
        ".aws",
        ".config",
        ".gnupg",
        ".azure",
        ".password-store",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "1password",
        "bitwarden",
        "keyrings",
        "com.agilebits.onepassword7",
        "com.1password.1password",
        "bravesoftware",
        "microsoft edge",
        "arc",
        "opera",
        "keychains",
        "cookies",
        "login data",
        "local state",
        "firefox",
        "safari",
        "chromium",
        "google",
    }
    if parts & forbidden or any(p.startswith(".env") for p in parts):
        raise PolicyError("Access to credentials, configuration, or browser profiles is blocked.")
    return path


def operation_error(stderr: str) -> str:
    if any(s in stderr.lower() for s in ("not authorized", "not permitted", "-1743", "assistive")):
        return (
            "macOS denied permission. Check System Settings > Privacy & Security > "
            "Automation, Accessibility, or Screen Recording for the launching app."
        )
    return "macOS operation failed. Check the application, path, and system permissions."
