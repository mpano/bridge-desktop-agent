"""The small panel in the top-right corner while Bridge takes meeting notes.

It never takes the keyboard from your meeting app, stays visible on every space, and is left
out of screen sharing so the others in the call don't see it. AppKit, main thread.
"""

from __future__ import annotations

import threading

import AppKit as AK
from Foundation import NSMakeRect

from app.desktop.floating import FloatingPage


class MeetingPanel(FloatingPage):
    PAGE = "meeting.html"
    WIDTH, HEIGHT = 340, 230

    def __init__(self, controller, service):
        super().__init__(controller, service)
        self.window.setSharingType_(AK.NSWindowSharingNone)
        self.window.setCollectionBehavior_(
            AK.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AK.NSWindowCollectionBehaviorFullScreenAuxiliary
        )

    def place(self) -> None:
        visible = AK.NSScreen.mainScreen().visibleFrame()
        x = visible.origin.x + visible.size.width - self.WIDTH - 14
        y = visible.origin.y + visible.size.height - self.HEIGHT - 10
        self.window.setFrame_display_(NSMakeRect(x, y, self.WIDTH, self.HEIGHT), True)

    def appear(self) -> None:
        if self.controller.quitting:
            return
        if not self.loaded:
            self.load()
        self.place()
        self.window.orderFrontRegardless()
        self.send("window.BridgeMeetingPanel && window.BridgeMeetingPanel.poll()")

    def resigned(self) -> None:
        pass  # Stays while you're in your meeting app.

    def show(self) -> None:
        self.appear()

    def on_message(self, message: dict) -> None:
        if message.get("action") == "open":
            window = getattr(self.controller, "window", None)
            if window is not None:
                window.show("meetings")
            return
        super().on_message(message)


class MeetingHud:
    """The meeting engine's handle on the panel, from any thread."""

    def __init__(self, panel: MeetingPanel):
        self.panel = panel

    def _on_main(self, work) -> None:
        from PyObjCTools import AppHelper

        done = threading.Event()

        def run():
            try:
                work()
            finally:
                done.set()

        AppHelper.callAfter(run)
        done.wait(2.0)

    def show(self) -> None:
        self._on_main(self.panel.appear)

    def finished(self, _meeting_id: str) -> None:
        # The page shows "Notes ready" and closes itself after a while.
        self._on_main(lambda: self.panel.send("window.BridgeMeetingPanel.poll()"))

    def recording(self) -> bool:
        return bool(self.panel.window.isVisible())
