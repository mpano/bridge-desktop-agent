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
    assert panel.dashboard_button.title() == "Review action in dashboard"
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
