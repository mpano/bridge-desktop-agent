import logging

import structlog

from app.agent.agent import Agent
from app.agent.executor import Executor
from app.agent.planner import Planner
from app.agent.proactive import Proactive
from app.agent.scheduler import Scheduler
from app.assistant.day_plan import DayPlanner
from app.assistant.triage import InboxTriage
from app.integrations.accounts import AccountManager
from app.integrations.activity import SQLiteActivity
from app.integrations.email import GmailService
from app.integrations.slack import SlackService
from app.llm import prompts
from app.llm.factory import create_llm
from app.memory.facts import FactStore
from app.memory.store import SQLiteMemory
from app.preferences import service as preferences_service
from app.security.privacy import ToolResultPrivacy
from app.tools.browser import browser, chrome
from app.tools.email import compose
from app.tools.files import files, finder, projects, search
from app.tools.integrations.register import register as register_integrations
from app.tools.macos.applescript import MacOSAppleScript, NativeRunner
from app.tools.productivity import (
    briefing,
    calendar_mac,
    contacts,
    messages,
    notes,
    planning,
    reminders,
)
from app.tools.productivity import memory as memory_tools
from app.tools.registry import ToolRegistry
from app.tools.screen import screenshot
from app.tools.spotify import spotify
from app.tools.system import (
    apps,
    clipboard,
    discovery,
    mac,
    notifications,
    processes,
    schedules,
    shortcuts,
    volume,
)
from app.tools.system import proactive as proactive_tools
from app.tools.terminal import terminal
from app.workflows import retention
from app.workflows.schedules import ScheduleStore
from app.workflows.store import SQLiteWorkflows
from app.workflows.watches import ProactiveStore


def build_agent(settings, llm=None, runner=None, accounts=None):
    logging.basicConfig(level=settings.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
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
    accounts = accounts or AccountManager(settings, activity=SQLiteActivity(settings.database_path))
    spotify_app = spotify.SpotifyController(runner, script)
    people = contacts.ContactsDirectory()
    if settings.integrations_enabled:
        register_integrations(
            registry, accounts, desktop_spotify=spotify_app.play_uri, contacts=people
        )
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
    reminders_controller = reminders.RemindersController(runner)
    reminders.register(registry, reminders_controller)
    notes.register(registry, notes.NotesController(runner))
    calendar_controller = calendar_mac.MacCalendarController()
    calendar_mac.register(registry, calendar_controller)
    shortcuts.register(registry, shortcuts.ShortcutsController(runner, settings.shortcuts_trusted))
    notifications.register(registry, notifications.NotificationController(script))
    browser_controller = browser.BrowserController(
        runner, settings.default_browser, default_provider=preferences.browser
    )
    browser.register(registry, browser_controller)
    chrome.register(registry, chrome.ChromeController(runner))
    schedule_store = ScheduleStore(settings.database_path)
    schedules.register(registry, schedules.ScheduleController(schedule_store))
    mac_controller = mac.MacController(runner)
    mac.register(registry, mac_controller)
    connected = settings.integrations_enabled
    gmail_service = GmailService(accounts) if connected else None
    briefing.register(
        registry,
        briefing.BriefingController(
            calendar=calendar_controller,
            reminders=reminders_controller,
            mac=mac_controller,
            gmail=gmail_service,
        ),
    )
    proactive_store = ProactiveStore(settings.database_path)
    llm = llm or create_llm(settings)
    inbox = InboxTriage(gmail_service, llm) if gmail_service is not None else None
    planning.register(
        registry,
        planning.PlanningController(
            proactive_store,
            DayPlanner(proactive_store, calendar_controller, reminders_controller, inbox, llm),
            inbox,
            people,
        ),
        gmail_available=gmail_service is not None,
    )
    proactive_controller = proactive_tools.ProactiveController(
        proactive_store, schedule_store, accounts if connected else None
    )
    proactive_tools.register(registry, proactive_controller)
    compose.register(
        registry, compose.EmailComposer(runner, browser_controller.open_url, contacts=people)
    )
    contacts.register(registry, people)
    facts = FactStore(settings.database_path)
    memory_tools.register(registry, facts)
    prompts.memory_provider = facts.prompt_block
    messages.register(registry, messages.MessagesController(runner, people))
    files.register(registry, runner, preferences.editor)
    search.register(registry, search.FileSearchController())
    finder.register(registry, finder.FinderController(runner))
    terminal.register(registry, runner)
    spotify.register(registry, spotify_app)
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
        "spotify_set_volume",
        "spotify_play_uri",
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
    agent = Agent(
        Planner(
            llm,
            registry,
            privacy=ToolResultPrivacy(
                mode=settings.remote_tool_results,
                allowed_tools=set(settings.remote_tool_result_allowlist),
            ),
        ),
        Executor(registry),
        settings.max_rounds,
        workflows=workflows,
        preferences=preferences,
    )
    agent.accounts = accounts
    agent.memories = facts

    async def notify(title: str, message: str) -> None:
        await notifications.post_notification(runner, title, message)

    agent.scheduler = Scheduler(agent, schedule_store, notify)
    agent.proactive_store = proactive_store
    agent.proactive_controller = proactive_controller
    agent.proactive = Proactive(
        proactive_store,
        notify,
        calendar=calendar_controller,
        gmail=gmail_service,
        slack=SlackService(accounts) if connected else None,
        accounts=accounts if connected else None,
    )
    return agent
