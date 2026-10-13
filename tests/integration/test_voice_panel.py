"""Opt-in AppKit rendering checks. No microphone, model, network, or OS tools."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("BRIDGE_NATIVE_TESTS") != "1",
    reason="Opt-in native panel test (requires macOS WindowServer)",
)


def test_menu_bar_click_opens_the_compact_panel_not_a_dropdown(monkeypatch):
    import signal

    import AppKit as AK
    import rumps

    from app.desktop.floating import MenuPanel
    from app.desktop.service import ServiceState, ServiceStatus

    service = SimpleNamespace(
        port=8000, status=ServiceStatus(ServiceState.STOPPED, "Stopped"), tickets=Mock()
    )
    controller = SimpleNamespace(
        quitting=False, voice=None, window=None, update_dock=Mock(), refresh=Mock()
    )
    panel = MenuPanel(controller, service)
    app = rumps.App("Bridge UI test", title="Bridge test", quit_button=None)
    saved = signal.getsignal(signal.SIGINT)

    def attach():
        panel.attach(app._nsapp.nsstatusitem)
        assert app._nsapp.nsstatusitem.menu() is None
        assert app._nsapp.nsstatusitem.button().target() == panel.actions

    monkeypatch.setattr("rumps.rumps.AppHelper.runEventLoop", lambda: None)
    rumps.events.before_start.register(attach)
    try:
        app.run()
    finally:
        rumps.events.before_start.unregister(attach)
        if hasattr(app, "_nsapp") and hasattr(app._nsapp, "nsstatusitem"):
            AK.NSStatusBar.systemStatusBar().removeStatusItem_(app._nsapp.nsstatusitem)
        signal.signal(signal.SIGINT, saved)


def test_native_hotkey_registration_and_cleanup():
    from app.desktop.hotkey import GlobalVoiceShortcut

    callback = Mock()
    shortcut = GlobalVoiceShortcut(callback)
    try:
        registered = shortcut.start()
        assert registered or "unavailable" in shortcut.status
        callback.assert_not_called()
    finally:
        shortcut.close()
    assert not shortcut.key_ref.value
    assert not shortcut.handler_ref.value


def text_panel(monkeypatch):
    import AppKit as AK

    from app.desktop import text_panel as module
    from app.desktop.service import ServiceState, ServiceStatus

    AK.NSApplication.sharedApplication()
    service = SimpleNamespace(
        port=8000,
        status=ServiceStatus(ServiceState.STOPPED, None, ""),
        tickets=SimpleNamespace(issue=lambda: "t"),
    )
    controller = SimpleNamespace(
        quitting=False, submit_text=Mock(return_value=True), show_panel=Mock()
    )
    panel = module.TextPanel(controller, service, SimpleNamespace(screen_context_blocked_apps=[]))
    sent = []
    panel.send = lambda script: sent.append(script) if "BridgeText" in script else None
    pasted = []
    monkeypatch.setattr(module, "paste_text", pasted.append)
    monkeypatch.setattr(module.AppHelper, "callLater", lambda _delay, fn, *a: fn(*a))
    return panel, controller, sent, pasted


def test_text_panel_shows_the_text_once_the_page_is_ready(monkeypatch):
    import json

    panel, controller, sent, pasted = text_panel(monkeypatch)
    panel.open("hello", "Notes", allowed=True)
    assert sent == []  # The page isn't ready yet: it's kept for later.
    panel.on_message({"action": "text_ready"})
    payload = json.loads(sent[-1].split("show(", 1)[1].rsplit(")", 1)[0])
    assert payload == {"mode": "selection", "text": "hello", "app": "Notes", "allowed": True}
    panel.on_message({"action": "text_replace", "text": "Hello!"})
    assert pasted == ["Hello!"] and not panel.window.isVisible()
    panel.open("x", "Mail", allowed=False)
    assert '"allowed": false' in sent[-1]
    panel.on_message({"action": "text_ask", "request": "Create a reminder from this text:\n\nx"})
    controller.submit_text.assert_called_once()
    controller.show_panel.assert_called_once()


def test_text_panel_reads_the_whole_window_when_nothing_is_selected(monkeypatch):
    panel, controller, sent, _ = text_panel(monkeypatch)
    panel.on_message({"action": "text_ready"})
    panel.open_window("Google Chrome")
    assert '"reading": true' in sent[-1] and '"mode": "window"' in sent[-1]
    panel.fill_window({"text": "Article text", "window": "News", "url": "https://n.example/a"})
    assert '"text": "Article text"' in sent[-1] and '"title": "News"' in sent[-1]
    panel.window_failed("🔒 1Password is private, so Bridge won't read it.")
    assert "failed(" in sent[-1] and "private" in sent[-1]
    controller.submit_text.return_value = False
    panel.on_message({"action": "text_ask", "request": "Summarize"})
    assert "Bridge is busy" in sent[-1]
    panel.close()
    count = len(sent)
    panel.fill_window({"text": "late"})  # A read that finishes after closing is ignored.
    assert len(sent) == count
    panel.on_message({"action": "text_size", "height": 9999})
    assert panel.height == 640.0


def test_floating_panel_and_command_bar_follow_their_page():
    import AppKit as AK

    from app.desktop.floating import CommandBar, MenuPanel
    from app.desktop.service import ServiceState, ServiceStatus

    AK.NSApplication.sharedApplication()
    service = SimpleNamespace(
        port=8000, status=ServiceStatus(ServiceState.STOPPED, "Stopped"), tickets=Mock()
    )
    controller = SimpleNamespace(
        quitting=False, voice=None, window=None, update_dock=Mock(), open_dashboard=Mock()
    )
    panel = MenuPanel(controller, service)
    assert not panel.load() and not panel.loaded  # Waits for the service.
    panel.on_message({"action": "open", "view": "today"})
    controller.open_dashboard.assert_called_once()
    bar = CommandBar(controller, service)
    bar.on_message({"action": "size", "height": 300})
    assert bar.window.frame().size.height == 300
    bar.on_message({"action": "size", "height": 99999})
    assert bar.window.frame().size.height == 560  # Never taller than the screen allows.
    bar.on_message({"action": "unknown"})  # Ignored.
    panel.dock_off()
    controller.update_dock.assert_called_with(closing=panel)


def test_window_signs_itself_in_again_without_a_login_page():
    import AppKit as AK

    from app.desktop.window import BridgeWindow

    AK.NSApplication.sharedApplication()
    service = SimpleNamespace(
        port=8000, dashboard_url=Mock(return_value="http://localhost:8000/#launch=t")
    )
    window = BridgeWindow(SimpleNamespace(quitting=False, update_dock=Mock()), service)
    window.on_message({"action": "signin"})
    assert window.loaded and service.dashboard_url.call_count == 1
    loaded = window.web.URL()
    window.on_message({"action": "signin"})  # Again right away: ignored, no reload loop.
    assert service.dashboard_url.call_count == 1
    assert loaded is None or "auto=1" in str(loaded.absoluteString())


def test_text_panel_moves_when_its_top_bar_is_dragged():
    from app.desktop.service import ServiceState, ServiceStatus
    from app.desktop.text_panel import TextPanel

    service = SimpleNamespace(
        port=8000, status=ServiceStatus(ServiceState.STOPPED, "Stopped"), tickets=Mock()
    )
    panel = TextPanel(SimpleNamespace(quitting=False), service, SimpleNamespace())
    panel.window.setFrameOrigin_((200, 300))
    panel.on_message({"action": "panel_drag", "dx": 40, "dy": 25})  # Right and down.
    origin = panel.window.frame().origin
    assert (origin.x, origin.y) == (240, 275)
    panel.on_message({"action": "panel_drag", "dx": "nonsense"})
    panel.on_message({"action": "panel_drag", "dx": 99999, "dy": 0})
    origin = panel.window.frame().origin
    assert (origin.x, origin.y) == (240, 275)
