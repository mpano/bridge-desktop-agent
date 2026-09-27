import logging

import structlog

from app.agent.agent import Agent
from app.agent.executor import Executor
from app.agent.planner import Planner
from app.llm.factory import create_llm
from app.memory.store import SQLiteMemory
from app.preferences import service as preferences_service
from app.security.privacy import ToolResultPrivacy
from app.tools.browser import browser
from app.tools.files import files, finder, projects, search
from app.tools.macos.applescript import MacOSAppleScript, NativeRunner
from app.tools.registry import ToolRegistry
from app.tools.screen import screenshot
from app.tools.spotify import spotify
from app.tools.system import apps, clipboard, discovery, notifications, processes, volume
from app.tools.terminal import terminal
from app.workflows import retention
from app.workflows.store import SQLiteWorkflows


def build_agent(settings, llm=None, runner=None):
    logging.basicConfig(level=settings.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )
    runner = runner or NativeRunner()
    script = MacOSAppleScript(runner)
    registry = ToolRegistry()
    memory = SQLiteMemory(settings.database_path)
    preferences = preferences_service.ApplicationPreferences(
        memory, settings.default_browser, settings.default_editor
    )
    preferences_service.register(registry, preferences)
    apps.register(registry, runner, script)
    discovery.register(registry, discovery.ApplicationCatalog())
    projects.register(registry, memory)
    processes.register(registry, script)
    volume.register(registry, script)
    clipboard.register(registry, clipboard.ClipboardController(runner))
    notifications.register(registry, notifications.NotificationController(script))
    browser.register(
        registry,
        browser.BrowserController(
            runner, settings.default_browser, default_provider=preferences.browser
        ),
    )
    files.register(registry, runner, preferences.editor)
    search.register(registry, search.FileSearchController())
    finder.register(registry, finder.FinderController(runner))
    terminal.register(registry, runner)
    spotify.register(registry, spotify.SpotifyController(runner, script))
    screenshot.register(
        registry, screenshot.ScreenController(runner, settings.screenshot_directory)
    )
    # Explicit review list: future tools do not persist arguments by default.
    # URLs and web-search text can contain credentials and are intentionally excluded.
    for name in (
        "open_app",
        "activate_app",
        "close_app",
        "is_app_running",
        "list_running_apps",
        "get_volume",
        "set_volume",
        "open_folder",
        "open_file",
        "open_project",
        "create_folder",
        "run_terminal_command",
        "spotify_control",
        "take_screenshot",
        "remember_project",
        "lookup_project",
        "list_projects",
        "forget_project",
    ):
        registry.get(name).persist_arguments = True
    workflows = SQLiteWorkflows(
        settings.database_path, can_persist=lambda call: registry.get(call.name).persist_arguments
    )
    retention.register(registry, workflows)
    return Agent(
        Planner(
            llm or create_llm(settings),
            registry,
            privacy=ToolResultPrivacy(
                provider=settings.llm_provider,
                mode=settings.remote_tool_results,
                allowed_tools=set(settings.remote_tool_result_allowlist),
            ),
        ),
        Executor(registry),
        settings.max_rounds,
        workflows=workflows,
        preferences=preferences,
    )
