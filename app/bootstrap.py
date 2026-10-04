import logging
import sys

import structlog

from app.agent.agent import Agent
from app.agent.executor import Executor
from app.agent.planner import Planner
from app.agent.proactive import Proactive
from app.agent.scheduler import Scheduler
from app.assistant.briefs import Briefs
from app.assistant.chats import ChatStore
from app.assistant.commitments import CommitmentStore, CommitmentTracker
from app.assistant.day_plan import DayPlanner
from app.assistant.focus import FocusMode, FocusStore
from app.assistant.replies import ReplyDrafter
from app.assistant.slack_triage import SlackInbox
from app.assistant.triage import InboxTriage
from app.integrations.accounts import AccountManager
from app.integrations.activity import SQLiteActivity
from app.integrations.credentials import KeychainCredentials
from app.integrations.email import GmailService
from app.integrations.repos import RepoWatcher
from app.integrations.slack import SlackService
from app.integrations.spotify import SpotifyWebService
from app.integrations.tokens import MemoryTokens, TokenConnections
from app.integrations.work import GitHub, Jira
from app.llm import prompts
from app.llm.factory import create_llm
from app.memory.facts import FactStore
from app.memory.store import SQLiteMemory
from app.phone.access import PhoneAccess
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
    focus,
    messages,
    notes,
    planning,
    reminders,
)
from app.tools.productivity import commitments as commitment_tools
from app.tools.productivity import memory as memory_tools
from app.tools.registry import ToolRegistry
from app.tools.screen import context as screen_context
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
from app.tools.work import tools as work_tools
from app.tools.work.tools import GITHUB_READS, JIRA_READS
from app.workflows import retention
from app.workflows.schedules import ScheduleStore
from app.workflows.store import SQLiteWorkflows
from app.workflows.watches import ProactiveStore


def shared_tools(allowlist: list[str]) -> set[str]:
    """Sharing one of a service's reads means sharing all of them (new ones included)."""
    allowed = set(allowlist)
    for family in (JIRA_READS, GITHUB_READS):
        if allowed & set(family):
            allowed |= set(family)
    return allowed


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
    # Work tokens live in the Keychain alongside the other accounts (in memory when those are).
    keychain = isinstance(accounts.store, KeychainCredentials)
    tokens = TokenConnections(None if keychain else MemoryTokens(), activity=accounts.activity)
    jira, github = Jira(tokens), GitHub(tokens)
    repos = RepoWatcher(
        settings.database_path, github, projects=SQLiteMemory(settings.database_path)
    )
    work_tools.register(registry, jira, github, repos)
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
    day_planner = DayPlanner(proactive_store, calendar_controller, reminders_controller, inbox, llm)
    planning.register(
        registry,
        planning_controller := planning.PlanningController(
            proactive_store, day_planner, inbox, people
        ),
        gmail_available=gmail_service is not None,
    )
    focus_mode = FocusMode(
        FocusStore(settings.database_path),
        calendar=calendar_controller,
        accounts=accounts if connected else None,
        spotify_web=SpotifyWebService(accounts) if connected else None,
        spotify_desktop=spotify_app,
        gmail=gmail_service,
        slack=SlackService(accounts) if connected else None,
    )
    focus.register(registry, focus.FocusController(focus_mode))
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
    if sys.platform == "darwin":
        from app.desktop.screen_reader import MacScreenReader

        screen = screen_context.ScreenContextController(
            MacScreenReader(),
            llm,
            settings.screen_context_blocked_apps,
            settings.integrations_callback_port,
        )
        screen_context.register(registry, screen)
    else:
        screen = None
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
                allowed_tools=shared_tools(settings.remote_tool_result_allowlist),
            ),
        ),
        Executor(registry),
        settings.max_rounds,
        workflows=workflows,
        preferences=preferences,
    )
    agent.accounts = accounts
    agent.tokens, agent.jira, agent.github, agent.repos = tokens, jira, github, repos
    agent.memories = facts
    agent.use_chats(ChatStore(settings.database_path, days=settings.chat_retention_days))

    agent.phone = PhoneAccess(settings.database_path)

    async def notify(title: str, message: str, view: str = "today") -> None:
        await notifications.post_notification(runner, title, message, view)
        await agent.phone.notify(title, message, view)  # When you're away from the Mac.

    agent.scheduler = Scheduler(agent, schedule_store, notify)
    agent.schedule_store = schedule_store
    focus_mode.notify = notify
    agent.focus = focus_mode
    # For the Today screen.
    agent.calendar = calendar_controller
    agent.day_planner = day_planner
    agent.inbox = inbox
    agent.gmail = gmail_service
    agent.replies = ReplyDrafter(gmail_service, llm) if gmail_service is not None else None
    agent.slack_inbox = SlackInbox(accounts, SlackService(accounts), llm) if connected else None
    planning_controller.slack = agent.slack_inbox
    agent.screen = screen
    agent.proactive_store = proactive_store
    agent.proactive_controller = proactive_controller
    agent.proactive = Proactive(
        proactive_store,
        notify,
        calendar=calendar_controller,
        gmail=gmail_service,
        slack=SlackService(accounts) if connected else None,
        accounts=accounts if connected else None,
        focus=focus_mode,
    )
    commitments = CommitmentTracker(
        CommitmentStore(settings.database_path),
        llm,
        gmail=gmail_service,
        slack_inbox=agent.slack_inbox,
        replies=agent.replies,
    )
    commitments.notify = notify
    commitment_tools.register(registry, commitments)
    agent.commitments = commitments
    agent.proactive.commitments = commitments
    briefs = Briefs(
        settings.database_path,
        llm=llm,
        proactive_store=proactive_store,
        calendar=calendar_controller,
        commitments=commitments,
        inbox=inbox,
        slack_inbox=agent.slack_inbox,
        day_planner=day_planner,
    )
    briefs.notify = notify
    briefs.jira, briefs.github = jira, github
    repos.notify = notify
    agent.proactive.repos = repos
    agent.briefs = briefs
    agent.proactive.briefs = briefs
    try:
        proactive_controller.upgrade_routines()
    except Exception:
        logging.getLogger(__name__).warning("Couldn't update the routines.", exc_info=True)
    return agent
