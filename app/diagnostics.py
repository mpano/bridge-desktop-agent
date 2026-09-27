"""Read-only configuration checks; never probe macOS privacy permissions."""

import os
import platform
import sys
from pathlib import Path
from typing import Literal, TypedDict

from app.config.settings import Settings


class DiagnosticCheck(TypedDict):
    name: str
    status: Literal["ok", "warning"]
    message: str


class ProviderReport(TypedDict):
    name: str
    model: str
    remote_tool_results: str
    remote_tool_result_allowlist: list[str]


class DiagnosticsReport(TypedDict):
    ready: bool
    checks: list[DiagnosticCheck]
    provider: ProviderReport


def collect_diagnostics(settings: Settings) -> DiagnosticsReport:
    """Inspect local prerequisites without commands, network requests, or writes.

    Access flags are advisory; they cannot establish effective SQLite access or
    macOS TCC grants. Reports deliberately omit configured paths and secret values.
    """
    checks: list[DiagnosticCheck] = []

    def check(name: str, passed: bool, success: str, failure: str) -> None:
        checks.append(
            DiagnosticCheck(
                name=name,
                status="ok" if passed else "warning",
                message=success if passed else failure,
            )
        )

    check(
        "macos",
        platform.system() == "Darwin",
        "Running on macOS.",
        "Native desktop tools require macOS.",
    )
    check(
        "python",
        sys.version_info >= (3, 12),
        "Python 3.12 or newer is available.",
        "Python 3.12 or newer is required.",
    )
    if settings.llm_provider == "openai":
        check(
            "openai_api_key",
            bool(settings.openai_api_key.get_secret_value().strip()),
            "An OpenAI API key is configured; validity has not been tested.",
            "Configure OPENAI_API_KEY to use the OpenAI provider.",
        )
    else:
        checks.append(
            DiagnosticCheck(
                name="local_llm",
                status="warning",
                message="Local Ollama is configured; no OpenAI API key is needed. "
                "Server availability and model installation have not been tested.",
            )
        )
    check(
        "api_token",
        bool(settings.api_token.get_secret_value().strip()),
        "A local API token is configured.",
        "Configure API_TOKEN to authenticate API requests.",
    )
    try:
        parent = settings.database_path.expanduser().parent
        accessible = parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    except (OSError, RuntimeError, ValueError):
        accessible = False
    check(
        "database_directory",
        accessible,
        "Database parent directory exists and appears writable; database access is untested.",
        "Database parent directory is missing or does not appear writable.",
    )
    for executable in ("open", "osascript", "pbcopy", "pbpaste", "screencapture"):
        directory = "/usr/sbin" if executable == "screencapture" else "/usr/bin"
        path = Path(directory) / executable
        try:
            available = path.is_file() and os.access(path, os.X_OK)
        except OSError:
            available = False
        check(
            executable,
            available,
            f"Native {executable} executable is available.",
            f"Native {executable} executable is unavailable.",
        )
    checks.append(
        DiagnosticCheck(
            name="macos_permissions",
            status="warning",
            message="Automation, Accessibility, Screen Recording, and notification permissions "
            "are not tested. macOS may request them when a relevant tool is used.",
        )
    )
    return DiagnosticsReport(
        ready=not any(c["status"] == "warning" for c in checks),
        checks=checks,
        provider=ProviderReport(
            name=settings.llm_provider,
            model=(
                settings.local_llm_model
                if settings.llm_provider == "ollama"
                else settings.openai_model
            ),
            remote_tool_results=settings.remote_tool_results,
            remote_tool_result_allowlist=list(settings.remote_tool_result_allowlist),
        ),
    )
