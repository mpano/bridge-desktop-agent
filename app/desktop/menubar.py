"""Native Bridge menu bar: AppKit on the main thread, owned services on workers."""

import signal
import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

from app.config.settings import Settings
from app.desktop.service import LocalService, ServiceState
from app.desktop.voice import MenuVoiceService, VoiceState


class MenuBarController:
    """Inject native adapters so automated tests never touch real applications."""

    def __init__(
        self,
        service: LocalService,
        native,
        open_browser: Callable[[str], bool] = webbrowser.open,
        voice: MenuVoiceService | None = None,
    ):
        self.service = service
        self.voice = voice
        self.native = native
        self.open_browser = open_browser
        self.quitting = False
        icon = Path(__file__).with_name("assets") / "bridge-menubar.png"
        app_kwargs = {"title": "B", "quit_button": None}
        if icon.is_file():
            app_kwargs.update(icon=str(icon), title=None, template=True)
        self.app = native.App("Bridge", **app_kwargs)

        self.status_item = native.MenuItem("Service stopped")
        self.open_item = native.MenuItem("Open Bridge Dashboard", callback=self.open_dashboard)
        self.start_item = native.MenuItem("Start Bridge Service", callback=self.start)
        self.stop_item = native.MenuItem("Stop Bridge Service", callback=self.stop)

        self.voice_status_item = native.MenuItem("Voice: Off")
        self.voice_start_item = native.MenuItem(
            'Start listening for “Bridge”', callback=self.start_voice
        )
        self.voice_stop_item = native.MenuItem("Stop Voice Listening", callback=self.stop_voice)

        self.details_item = native.MenuItem("Bridge Details", callback=self.details)
        self.quit_item = native.MenuItem("Quit Bridge", callback=self.quit)
        menu = [
            self.status_item,
            None,
            self.open_item,
            self.start_item,
            self.stop_item,
        ]
        if self.voice is not None:
            menu += [
                None,
                self.voice_status_item,
                self.voice_start_item,
                self.voice_stop_item,
            ]
        menu += [None, self.details_item, self.quit_item]
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

    def open_dashboard(self, _=None) -> None:
        status = self.service.status
        if status.state == ServiceState.RUNNING and status.url:
            try:
                opened = self.open_browser(status.url)
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
        if self.voice is not None:
            self.voice.stop()
        self.service.stop()
        self.refresh(None)

    def _voice_finished(self) -> bool:
        return self.voice is None or self.voice.wait(timeout=0)

    def refresh(self, _=None) -> None:
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
                VoiceState.LISTENING: 'Voice: Listening for “Bridge”',
                VoiceState.STOPPING: "Voice: Stopping…",
                VoiceState.FAILED: "Voice: Needs attention",
            }[voice_status.state]
            self.voice_start_item.set_callback(
                self.start_voice
                if voice_status.state in {VoiceState.STOPPED, VoiceState.FAILED}
                and not self.quitting
                else None
            )
            self.voice_stop_item.set_callback(
                self.stop_voice
                if voice_status.state in {VoiceState.STARTING, VoiceState.LISTENING}
                and not self.quitting
                else None
            )

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
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    from PyObjCTools import MachSignals

    local_service = LocalService(settings)
    voice_service = MenuVoiceService(settings, local_service)
    controller = MenuBarController(local_service, rumps, voice=voice_service)

    def prepare_native():
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
        for signum in (signal.SIGINT, signal.SIGTERM):
            MachSignals.signal(signum, lambda _: controller.quit())

    rumps.events.before_start.register(prepare_native)
    try:
        controller.run()
    finally:
        rumps.events.before_start.unregister(prepare_native)
