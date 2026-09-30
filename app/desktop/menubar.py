"""Native Bridge menu bar: AppKit on the main thread, owned services on workers."""

import os
import signal
import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

from app.config.settings import Settings
from app.desktop.login import LoginItem, running_app_bundle
from app.desktop.service import LocalService, ServiceState
from app.desktop.voice import IDLE_STATES, MenuVoiceService, VoiceState


class MenuBarController:
    """Inject native adapters so automated tests never touch real applications."""

    def __init__(
        self,
        service: LocalService,
        native,
        open_browser: Callable[[str], bool] = webbrowser.open,
        voice: MenuVoiceService | None = None,
        login: LoginItem | None = None,
    ):
        self.service = service
        self.voice = voice
        self.native = native
        self.open_browser = open_browser
        self.login = login
        self.quitting = False
        self.panel = None
        self.shortcut = None
        self.shortcut_status = "Shortcut off"
        icon = Path(__file__).with_name("assets") / "bridge-menubar.png"
        # macOS constrains status-item image height. Keep the Bridge wordmark visible
        # by pairing the template symbol with the product name instead of relying on
        # image pixels alone.
        app_kwargs = {"title": "Bridge", "quit_button": None}
        if icon.is_file():
            app_kwargs.update(icon=str(icon), template=True)
        self.app = native.App("Bridge", **app_kwargs)

        self.status_item = native.MenuItem("Service stopped")
        self.voice_status_item = native.MenuItem("Voice: Off")
        self.panel_item = native.MenuItem("Show Bridge Panel", callback=self.show_panel)
        self.speak_item = native.MenuItem("Speak Now", callback=self.shortcut_action)
        self.open_item = native.MenuItem("Open Dashboard", callback=self.open_dashboard)
        self.start_item = native.MenuItem("Start Bridge Service", callback=self.start)
        self.stop_item = native.MenuItem("Stop Bridge Service", callback=self.stop)
        self.voice_start_item = native.MenuItem(
            "Start listening for “Hey Bridge”", callback=self.start_voice
        )
        self.voice_stop_item = native.MenuItem("Stop Voice Listening", callback=self.stop_voice)
        self.login_item = native.MenuItem("Open at Login", callback=self.toggle_login)
        self.details_item = native.MenuItem("Bridge Details…", callback=self.details)
        self.quit_item = native.MenuItem("Quit Bridge", callback=self.quit)

        # Right-click menu once the panel owns left-click; the whole menu before that.
        menu = [self.status_item]
        if self.voice is not None:
            menu.append(self.voice_status_item)
        menu += [None, self.panel_item]
        if self.voice is not None:
            menu.append(self.speak_item)
        menu += [self.open_item, None, self.start_item, self.stop_item]
        if self.voice is not None:
            menu += [self.voice_start_item, self.voice_stop_item]
        menu += [None]
        if self.login is not None:
            menu.append(self.login_item)
        menu += [self.details_item, self.quit_item]
        self.app.menu = menu
        self.timer = native.Timer(self.refresh, 0.25)
        self.refresh(None)

    def start(self, _=None) -> None:
        if not self.quitting:
            self.service.start()
            self.refresh(None)

    def stop(self, _=None) -> None:
        if self.voice is not None:
            self.voice.stop()
        self.service.stop()
        self.refresh(None)

    def start_voice(self, _=None) -> None:
        if self.voice is not None and not self.quitting:
            self.voice.start()
            self.refresh(None)

    def stop_voice(self, _=None) -> None:
        if self.voice is not None:
            self.voice.stop()
            self.refresh(None)

    def speak_once(self, _=None) -> None:
        if self.voice is not None and not self.quitting:
            self.voice.start(once=True)
            self.refresh(None)

    def train_voice(self, _=None) -> bool:
        if self.voice is None or self.quitting:
            return False
        started = self.voice.train_wake_word()
        self.refresh(None)
        return started

    def submit_text(self, text: str) -> bool:
        if self.voice is None or self.quitting:
            return False
        submitted = self.voice.submit_text(text)
        self.refresh(None)
        return submitted

    def shortcut_action(self, _=None) -> None:
        if self.quitting or self.voice is None:
            return
        if not self.voice.active and self.voice.pending_review is None:
            self.speak_once()
        self.show_panel()

    def show_panel(self, _=None) -> None:
        if self.panel is not None and not self.panel.window.isVisible():
            self.panel.toggle()

    def toggle_login(self, _=None) -> None:
        if self.login is None or not self.login.available:
            return
        try:
            self.login.set_enabled(not self.login.enabled)
        except OSError:
            self.native.alert(
                title="Bridge", message="Could not update Open at Login. Check ~/Library access."
            )
        self.refresh(None)

    def open_microphone_settings(self, _=None) -> None:
        self.open_browser(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
        )

    def open_voice_guide(self, _=None) -> None:
        self.open_browser((Path(__file__).resolve().parents[2] / "docs/VOICE.md").as_uri())

    def open_wakeword_guide(self, _=None) -> None:
        self.open_browser(
            (Path(__file__).resolve().parents[2] / "docs/WAKEWORD_BRIDGE.md").as_uri()
        )

    def open_dashboard(self, _=None) -> None:
        status = self.service.status
        if status.state == ServiceState.RUNNING and status.url:
            try:
                opened = self.open_browser(self.service.dashboard_url() or status.url)
            except (OSError, webbrowser.Error):
                opened = False
            if not opened:
                self.native.alert(title="Bridge", message=f"Open {status.url} in your browser.")

    def details(self, _=None) -> None:
        status = self.service.status
        message = status.message
        if status.url:
            message += f"\n\nDashboard: {status.url}\nConnect with API_TOKEN from your .env file."
        if self.voice is not None:
            message += f"\n\nVoice: {self.voice.status.message}"
        self.native.alert(title="Bridge", message=message)

    def quit(self, _=None) -> None:
        self.quitting = True
        if self.shortcut is not None:
            self.shortcut.close()
        if self.voice is not None:
            self.voice.stop()
        self.service.stop()
        self.refresh(None)

    def _voice_finished(self) -> bool:
        return self.voice is None or self.voice.wait(timeout=0)

    def refresh(self, _=None) -> None:
        if self.shortcut is not None:
            self.shortcut_status = self.shortcut.status
        status = self.service.status
        self.status_item.title = f"Service: {status.state.value.capitalize()}"
        self.open_item.set_callback(
            self.open_dashboard
            if status.state == ServiceState.RUNNING and not self.quitting
            else None
        )
        self.start_item.set_callback(
            self.start
            if status.state in {ServiceState.STOPPED, ServiceState.FAILED} and not self.quitting
            else None
        )
        self.stop_item.set_callback(
            self.stop
            if status.state in {ServiceState.STARTING, ServiceState.RUNNING} and not self.quitting
            else None
        )

        if self.voice is not None:
            voice_status = self.voice.status
            self.voice_status_item.title = {
                VoiceState.STOPPED: "Voice: Off",
                VoiceState.STARTING: "Voice: Starting…",
                VoiceState.LISTENING: "Voice: Listening for “Hey Bridge”",
                VoiceState.STOPPING: "Voice: Stopping…",
                VoiceState.FAILED: "Voice: Needs attention",
            }.get(voice_status.state, f"Voice: {voice_status.state.value.capitalize()}")
            self.voice_start_item.set_callback(
                self.start_voice
                if voice_status.state in IDLE_STATES and not self.quitting
                else None
            )
            self.voice_stop_item.set_callback(
                self.stop_voice
                if voice_status.state not in IDLE_STATES | {VoiceState.STOPPING}
                and not self.quitting
                else None
            )
            self.speak_item.set_callback(
                self.shortcut_action
                if not self.voice.active
                and self.voice.pending_review is None
                and status.state == ServiceState.RUNNING
                and not self.quitting
                else None
            )

        self.panel_item.set_callback(
            self.show_panel if self.panel is not None and not self.quitting else None
        )
        if self.login is not None:
            self.login_item.state = int(self.login.enabled)
            self.login_item.set_callback(
                self.toggle_login if self.login.available and not self.quitting else None
            )

        if self.panel is not None:
            self.panel.refresh()
            if self.voice is not None:
                self.voice.panel_visible = bool(self.panel.window.isVisible())

        if self.quitting and status.state in {ServiceState.STOPPED, ServiceState.FAILED}:
            if self.service.wait(timeout=0) and self._voice_finished():
                self.timer.stop()
                self.native.quit_application()

    def run(self) -> None:
        previous = {}
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, lambda *_: self.quit())
            self.start()
            self.timer.start()
            self.app.run()
        finally:
            self.timer.stop()
            if self.shortcut is not None:
                self.shortcut.close()
            if self.voice is not None:
                self.voice.stop()
            self.service.stop()
            voice_done = self.voice is None or self.voice.wait(timeout=15)
            service_done = self.service.wait(timeout=35)
            if not voice_done or not service_done:
                print("Bridge is still finishing shutdown; a worker remains active.")
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def run_menubar(settings: Settings) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("The Bridge menu-bar launcher requires macOS.")
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("The Bridge menu-bar launcher must run on the main thread.")
    try:
        import rumps
    except ImportError as exc:
        raise RuntimeError(
            "Install the menu-bar dependency: python -m pip install -e '.[menubar]'"
        ) from exc
    import objc
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory, NSOperationQueue
    from Foundation import NSBundle
    from PyObjCTools import MachSignals

    # The development launcher runs inside Python.app; show "Bridge" in the menu bar,
    # About panel and Dock instead of "Python".
    try:
        NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "Bridge"
    except (TypeError, AttributeError):
        pass

    from app.tools.macos.applescript import NativeRunner
    from app.tools.system.notifications import post_notification

    local_service = LocalService(settings)
    voice_service = MenuVoiceService(settings, local_service)
    runner = NativeRunner()

    async def notify(title: str, message: str) -> None:
        await post_notification(runner, title, message)

    voice_service.notifier = notify
    controller = MenuBarController(
        local_service, rumps, voice=voice_service, login=LoginItem(running_app_bundle())
    )

    def prepare_native():
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
        from app.desktop.panel import VoicePanel, install_main_menu

        controller.panel = VoicePanel(controller, settings)
        install_main_menu(controller.panel.actions)

        def should_terminate(delegate, sender):
            # ⌘Q and the Dock's Quit go through Bridge's orderly shutdown first.
            stopped = controller.service.status.state in {ServiceState.STOPPED, ServiceState.FAILED}
            if controller.quitting and stopped:
                return 1  # NSTerminateNow
            controller.quit()
            return 0  # NSTerminateCancel; quit() terminates once workers have stopped.

        objc.classAddMethods(
            type(controller.app._nsapp),
            [
                objc.selector(
                    should_terminate,
                    selector=b"applicationShouldTerminate:",
                    signature=b"Q@:@",
                )
            ],
        )
        # rumps 0.4 initializes its NSStatusItem before emitting before_start.
        controller.panel.attach(controller.app._nsapp.nsstatusitem)
        if settings.voice_shortcut_enabled:
            from app.desktop.hotkey import GlobalVoiceShortcut

            controller.shortcut = GlobalVoiceShortcut(controller.shortcut_action)
            controller.shortcut.start()
            controller.shortcut_status = controller.shortcut.status
        for signum in (signal.SIGINT, signal.SIGTERM):
            MachSignals.signal(signum, lambda _: controller.quit())

        def after_launch():
            # AppKit installs its own Apple event handlers while finishing launch; ours go after.
            controller.panel.actions.listenForReopen()
            # A menu-bar app has no window, so a deliberate launch shows the panel right away.
            # The icon may also be hidden behind the notch on a crowded menu bar.
            if os.environ.get("BRIDGE_LAUNCHED_AT_LOGIN") != "1":
                controller.panel.show()

        NSOperationQueue.mainQueue().addOperationWithBlock_(after_launch)

    rumps.events.before_start.register(prepare_native)
    try:
        controller.run()
    finally:
        rumps.events.before_start.unregister(prepare_native)
