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


def test_text_popup_explains_missing_permission_and_needs_text():
    import AppKit as AK

    from app.desktop.text_popup import TextPopup

    AK.NSApplication.sharedApplication()
    controller = SimpleNamespace(
        service=SimpleNamespace(status=None), submit_text=Mock(), show_panel=Mock()
    )
    popup = TextPopup(controller, SimpleNamespace(api_token=SimpleNamespace(get_secret_value=str)))
    popup.open("", "Mail", allowed=False)
    assert "Accessibility" in popup.status.stringValue()
    assert popup.result_buttons["Ask Bridge"].title() == "Allow…"
    assert not popup.result_buttons["Replace"].isEnabled()
    popup.run("improve")
    assert "no text" in popup.status.stringValue()
    popup.close()
    popup.open("hello", "Notes", allowed=True)
    assert popup.source_label.stringValue() == "Selected in Notes"
    popup.run("remind")
    controller.submit_text.assert_called_once()
    assert "Create a reminder" in controller.submit_text.call_args.args[0]
    popup.close()


def test_text_popup_acts_on_the_whole_window_when_nothing_is_selected():
    import AppKit as AK

    from app.desktop.text_popup import TextPopup

    AK.NSApplication.sharedApplication()
    controller = SimpleNamespace(
        service=SimpleNamespace(status=None), submit_text=Mock(return_value=True), show_panel=Mock()
    )
    popup = TextPopup(controller, SimpleNamespace(api_token=SimpleNamespace(get_secret_value=str)))
    popup.open_window("Google Chrome")
    assert popup.source_label.stringValue() == "Whole window in Google Chrome"
    assert "reading" in popup.status.stringValue() and popup.busy
    popup.fill_window({"text": "Article text", "window": "News", "url": "https://n.example/a"})
    assert popup.input.string() == "Article text" and not popup.busy
    popup.output.setString_("A summary")
    popup._refresh_buttons()
    assert not popup.result_buttons["Replace"].isEnabled()  # No selection to replace.
    assert popup.result_buttons["Copy"].isEnabled()
    popup.run("remind")
    assert controller.submit_text.call_args.args[0] == "Remind me about “News” https://n.example/a"
    popup.open_window("1Password")
    popup.window_failed("🔒 1Password is private, so Bridge won't read it.")
    assert "private" in popup.status.stringValue() and not popup.busy
    popup.close()
    popup.fill_window({"text": "late"})  # A read that finishes after closing is ignored.
    assert popup.input.string() == ""


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
