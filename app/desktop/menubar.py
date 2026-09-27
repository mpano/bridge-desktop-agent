"""Small native adapter: AppKit on the main thread, ASGI on an owned worker."""

import signal
import sys
import threading
import webbrowser
from collections.abc import Callable

from app.config.settings import Settings
from app.desktop.service import LocalService, ServiceState


class MenuBarController:
    """Inject rumps and browser opening so automated tests never touch real applications."""

    def __init__(
        self, service: LocalService, native, open_browser: Callable[[str], bool] = webbrowser.open
    ):
        self.service = service
        self.native = native
        self.open_browser = open_browser
        self.quitting = False
        self.app = native.App("Desktop Agent", title="DA", quit_button=None)
        self.status_item = native.MenuItem("Service stopped")
        self.open_item = native.MenuItem("Open Dashboard", callback=self.open_dashboard)
        self.start_item = native.MenuItem("Start Service", callback=self.start)
        self.stop_item = native.MenuItem("Stop Service", callback=self.stop)
        self.details_item = native.MenuItem("Service Details", callback=self.details)
        self.quit_item = native.MenuItem("Quit Desktop Agent", callback=self.quit)
        self.app.menu = [
            self.status_item,
            None,
            self.open_item,
            self.start_item,
            self.stop_item,
            None,
            self.details_item,
            self.quit_item,
        ]
        self.timer = native.Timer(self.refresh, 0.25)
        self.refresh(None)

    def start(self, _=None) -> None:
        if not self.quitting:
            self.service.start()
            self.refresh(None)

    def stop(self, _=None) -> None:
        self.service.stop()
        self.refresh(None)

    def open_dashboard(self, _=None) -> None:
        status = self.service.status
        if status.state == ServiceState.RUNNING and status.url:
            # The URL contains no bearer token. Authentication stays in the dashboard.
            try:
                opened = self.open_browser(status.url)
            except (OSError, webbrowser.Error):
                opened = False
            if not opened:
                self.native.alert(
                    title="Desktop Agent", message=f"Open {status.url} in your browser."
                )

    def details(self, _=None) -> None:
        status = self.service.status
        message = status.message
        if status.url:
            message += f"\n\nDashboard: {status.url}\nConnect with API_TOKEN from your .env file."
        self.native.alert(title="Desktop Agent", message=message)

    def quit(self, _=None) -> None:
        self.quitting = True
        self.service.stop()
        self.refresh(None)

    def refresh(self, _=None) -> None:
        status = self.service.status
        self.status_item.title = f"Service: {status.state.value.capitalize()}"
        self.app.title = "DA" if status.state == ServiceState.RUNNING else "DA ·"
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
        if self.quitting and status.state in {ServiceState.STOPPED, ServiceState.FAILED}:
            # Status can change just before the worker exits; avoid quitting AppKit prematurely.
            if self.service.wait(timeout=0):
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
            self.service.stop()
            if not self.service.wait(timeout=35):
                print(
                    "Desktop Agent is still finishing shutdown; the service worker remains active."
                )
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def run_menubar(settings: Settings) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("The menu-bar launcher requires macOS.")
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("The menu-bar launcher must run on the main thread.")
    try:
        import rumps
    except ImportError as exc:
        raise RuntimeError(
            "Install the menu-bar dependency: python -m pip install -e '.[menubar]'"
        ) from exc
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    from PyObjCTools import MachSignals

    controller = MenuBarController(LocalService(settings), rumps)

    def prepare_native():
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
        # rumps installs its own SIGINT handler just before this event. Replace it with
        # a run-loop-aware handler so Ctrl-C/SIGTERM follows our graceful quit path.
        for signum in (signal.SIGINT, signal.SIGTERM):
            MachSignals.signal(signum, lambda _: controller.quit())

    rumps.events.before_start.register(prepare_native)
    try:
        controller.run()
    finally:
        rumps.events.before_start.unregister(prepare_native)
