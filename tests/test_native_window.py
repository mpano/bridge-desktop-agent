"""The Bridge window's link rules and the menu bar icon states (AppKit pieces faked)."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.desktop.service import ServiceState, ServiceStatus
from app.security.confirmation import ConfirmationStore

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")


@pytest.mark.parametrize(
    ("url", "inside"),
    [
        ("http://localhost:8000/#launch=abc", True),
        ("http://127.0.0.1:8000/api/v1/focus", True),
        ("about:blank", True),
        ("http://localhost:9999/", False),  # Another local server is not Bridge.
        ("https://accounts.google.com/o/oauth2/v2/auth", False),
        ("https://slack.com/oauth/v2/authorize", False),
        ("http://localhost.evil.example:8000/", False),
    ],
)
def test_only_bridge_pages_load_inside_the_window(url, inside):
    from app.desktop.window import is_local

    assert is_local(url, 8000) is inside


def test_pending_approvals_are_counted_until_they_expire(monkeypatch):
    store = ConfirmationStore(ttl=300)
    call = SimpleNamespace(model_copy=lambda deep: "call")
    store.create(SimpleNamespace(request_id="r"), call)
    assert store.count() == 1
    clock = __import__("time").monotonic() + 301
    monkeypatch.setattr("app.security.confirmation.time.monotonic", lambda: clock)
    assert store.count() == 0


def make_menu(pending=0, voice_state=None):
    from app.desktop.menubar import MenuBarController
    from tests.test_desktop import FakeApp, FakeItem, FakeTimer

    native = SimpleNamespace(
        App=FakeApp, MenuItem=FakeItem, Timer=FakeTimer, alert=Mock(), quit_application=Mock()
    )
    service = Mock()
    service.status = ServiceStatus(ServiceState.RUNNING, "Running", "http://127.0.0.1:8000")
    service.pending_approvals = lambda: pending
    voice = None
    if voice_state is not None:
        voice = Mock()
        voice.status = SimpleNamespace(state=voice_state, message="")
    return MenuBarController(service, native, Mock(), voice=voice)


def test_menu_bar_icon_shows_what_bridge_is_doing():
    from app.desktop.status_icon import LISTENING, NEEDS_YOU, READY
    from app.desktop.voice import VoiceState

    assert make_menu().status_state() == READY
    assert make_menu(pending=2).status_state() == NEEDS_YOU
    assert make_menu(voice_state=VoiceState.APPROVAL).status_state() == NEEDS_YOU
    assert make_menu(voice_state=VoiceState.RECORDING).status_state() == LISTENING
    dictating = make_menu()
    dictating.dictation = SimpleNamespace(state="listening")
    assert dictating.status_state() == LISTENING


def test_status_images_are_templates_only_when_plain():
    from app.desktop.status_icon import NEEDS_YOU, READY, status_image

    assert status_image(READY).isTemplate()
    assert not status_image(NEEDS_YOU).isTemplate()
    assert status_image(READY).size().width == 18


def test_dock_icon_stays_while_any_bridge_window_is_open():
    menu = make_menu()
    shown = lambda visible: SimpleNamespace(  # noqa: E731
        window=SimpleNamespace(isVisible=lambda: visible, isMiniaturized=lambda: False)
    )
    menu.panel = shown(False)
    menu.panel.set_dock_visible = Mock()
    menu.window = shown(True)
    menu.update_dock()
    menu.panel.set_dock_visible.assert_called_with(True)
    menu.update_dock(closing=menu.window)  # Closing the window: nothing else is open.
    menu.panel.set_dock_visible.assert_called_with(False)
