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


@pytest.fixture
def panel(tmp_path):
    import AppKit as AK

    from app.config.settings import Settings
    from app.desktop.panel import VoicePanel
    from app.desktop.service import ServiceState, ServiceStatus
    from app.desktop.voice import MenuVoiceService

    AK.NSApplication.sharedApplication()
    settings = Settings(
        _env_file=None, api_token="test-only", voice_wake_word_model_path=tmp_path / "missing.onnx"
    )
    service = SimpleNamespace(
        status=ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")
    )
    voice = MenuVoiceService(settings, service)
    controller = SimpleNamespace(
        voice=voice,
        service=service,
        quitting=False,
        **{
            name: Mock()
            for name in (
                "speak_once",
                "stop_voice",
                "start_voice",
                "open_dashboard",
                "start",
                "open_microphone_settings",
                "open_voice_guide",
                "open_wakeword_guide",
                "quit",
                "details",
                "submit_text",
                "train_voice",
            )
        },
    )
    view = VoicePanel(controller, settings)
    yield view
    view.window.close()


def test_native_buttons_dispatch_without_auto_start(panel):
    import AppKit as AK

    assert panel.speak_button.isEnabled()
    assert not panel.wake_button.isEnabled()
    panel.controller.speak_once.assert_not_called()
    AK.NSApplication.sharedApplication().sendAction_to_from_(
        panel.speak_button.action(), panel.speak_button.target(), panel.speak_button
    )
    panel.controller.speak_once.assert_called_once_with()
    assert not panel.controller.voice.active


def test_native_panel_states_render_and_preserve_approval_action(panel):
    from app.desktop.voice import VoiceState

    status_item = Mock()
    panel.attach(status_item)
    status_item.setMenu_.assert_called_once_with(None)
    for state in VoiceState:
        panel.controller.voice._update(state, "Test status")
        panel.refresh()
        assert panel.state_label.stringValue()
    panel.controller.voice._update(VoiceState.APPROVAL, "Review in dashboard")
    panel.refresh()
    assert panel.dashboard_button.isEnabled()
    panel.controller.quitting = True
    panel.refresh()
    assert not panel.speak_button.isEnabled()
    assert not panel.wake_button.isEnabled()
    assert not panel.dashboard_button.isEnabled()


def test_native_view_renders_offscreen(panel):
    import AppKit as AK

    bitmap = panel.view.bitmapImageRepForCachingDisplayInRect_(panel.view.bounds())
    panel.view.cacheDisplayInRect_toBitmapImageRep_(panel.view.bounds(), bitmap)
    image = bitmap.representationUsingType_properties_(AK.NSBitmapImageFileTypePNG, {})
    assert image.length() > 1000
    assert bitmap.pixelsWide() >= panel.WIDTH


def test_rumps_status_item_hosts_panel_without_dropdown(panel, monkeypatch):
    import signal

    import AppKit as AK
    import rumps

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


def test_wake_switch_can_stop_listener_and_never_enables_missing_model(panel):
    import AppKit as AK

    voice = panel.controller.voice
    panel.refresh()
    assert not panel.wake_button.isEnabled()
    assert panel.wake_button.state() == AK.NSControlStateValueOff
    assert "your voice" in panel.wake_detail.stringValue()
    assert panel.train_button.isEnabled()
    # Model and worker are fake; this represents an explicitly enabled wake session.
    voice._thread = SimpleNamespace(is_alive=lambda: True)
    voice._once = False
    panel.refresh()
    assert panel.wake_button.isEnabled()
    assert panel.wake_button.state() == AK.NSControlStateValueOn
    AK.NSApplication.sharedApplication().sendAction_to_from_(
        panel.wake_button.action(), panel.wake_button.target(), panel.wake_button
    )
    panel.controller.stop_voice.assert_called_once_with()
    panel.controller.start_voice.assert_not_called()
    voice._once = True
    panel.refresh()
    assert not panel.wake_button.isEnabled()
    assert panel.wake_button.state() == AK.NSControlStateValueOff


def test_result_badge_requires_actual_result_status(panel):
    from app.desktop.voice import VoiceState, VoiceStatus

    voice = panel.controller.voice
    voice._status = VoiceStatus(VoiceState.READY, "Ready", "Open Spotify", "Response text")
    panel.refresh()
    assert "Done" not in panel.outcome_label.stringValue()
    voice._status = VoiceStatus(VoiceState.READY, "Ready", "Open Spotify", "Opened", "completed")
    panel.refresh()
    assert "Done" in panel.outcome_label.stringValue()
    voice._status = VoiceStatus(VoiceState.READY, "Ready", "Open Spotify", "Failed", "failed")
    panel.refresh()
    assert "Needs attention" in panel.outcome_label.stringValue()


def test_compact_panel_keeps_footer_in_scrollable_document(panel):
    from Foundation import NSMakePoint

    panel.scroll.setFrameSize_((panel.WIDTH, 480))
    panel.view.scrollPoint_(NSMakePoint(0, panel.HEIGHT - 480))
    assert panel.view.isFlipped()
    assert panel.scroll.hasVerticalScroller()
    assert panel.scroll.documentVisibleRect().origin.y > 0
    assert panel.service_button.frame().origin.y < panel.view.bounds().size.height


def test_approval_sheet_shows_full_arguments_and_enter_defaults_to_decline():
    import AppKit as AK

    from app.desktop.approval import ApprovalSheet
    from app.desktop.voice import ApprovalView

    arguments = '{"path": "' + ("long directory/" * 200) + 'reports"}'
    decision = Mock()
    sheet = ApprovalSheet(
        ApprovalView("review-id", "create_folder", arguments, "Create?", 99999), decision
    )
    assert sheet.arguments_view.string() == arguments
    assert sheet.alert.buttons()[0].title() == "Decline"
    assert sheet.alert.buttons()[1].keyEquivalent() == ""
    sheet.decide(AK.NSAlertFirstButtonReturn)
    decision.assert_called_once_with("review-id", False)


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


def test_window_is_normal_minimizable_and_pin_is_remembered(panel, tmp_path, monkeypatch):
    import AppKit as AK

    from app.desktop import panel as panel_module

    monkeypatch.setattr(panel_module, "PREFERENCES", tmp_path / "panel.json")
    assert panel.window.level() == AK.NSNormalWindowLevel
    assert panel.window.styleMask() & AK.NSWindowStyleMaskMiniaturizable
    panel.preferences = {}
    panel.toggle_pin()
    assert panel.window.level() == AK.NSFloatingWindowLevel
    assert panel_module.load_preferences() == {"pinned": True}
    panel.toggle_pin()
    assert panel.window.level() == AK.NSNormalWindowLevel


def test_quick_actions_and_history_render(panel):
    panel.quick_buttons[0].performClick_(None)
    panel.controller.submit_text.assert_called_once_with("Plan my day")
    voice = panel.controller.voice
    voice._history = [
        {"at": 1.0, "request": "Brief me", "reply": "Here's your day", "status": "completed"}
    ]
    panel.refresh()
    assert "Brief me" in panel.history_view.string()
    assert "Here's your day" in panel.history_view.string()


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
