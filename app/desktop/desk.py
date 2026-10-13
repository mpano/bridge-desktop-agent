"""Two small things on your screen: the "What's this?" card and Bridge on your desk.

What's this? (⌃⌥W): a card next to the pointer explains what's under it, while the overlay
highlights the spot. Clicking anywhere else closes it.

Bridge on your desk: your avatar in a corner of the screen. It's as small as the avatar until
it has something to say (a bubble, the hover card), stays out of screenshots, hides while
Bridge itself is using the screen, and can be dragged anywhere (the spot is remembered).
AppKit work happens on the main thread.
"""

from __future__ import annotations

import AppKit as AK
import Quartz
from Foundation import NSMakeRect, NSObject, NSUserDefaults

from app.desktop.floating import FloatingPage, keep_private

POSITION_KEY = "BridgeDeskBuddyCorner"  # The avatar's bottom-right corner, in screen points.


def pointer() -> tuple[float, float]:
    """The pointer in Quartz points (top-left origin)."""
    point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return float(point.x), float(point.y)


class ExplainPanel(FloatingPage):
    """The card next to the pointer for "What's this?"."""

    PAGE = "explain.html"
    WIDTH, HEIGHT = 380, 250

    def __init__(self, controller, service):
        super().__init__(controller, service)
        keep_private(self.window)
        self.overlay = None
        self.point = (0.0, 0.0)
        self.height = float(self.HEIGHT)  # The card grows to fit its words.

    def place(self) -> None:
        screen = AK.NSScreen.mainScreen()
        full, visible = screen.frame(), screen.visibleFrame()
        x, y = self.point
        flipped_y = full.size.height - y  # AppKit's bottom-left origin.
        left = x + 24
        if left + self.WIDTH > visible.origin.x + visible.size.width - 8:
            left = x - 24 - self.WIDTH
        height = self.height
        bottom = flipped_y - 24 - height
        if bottom < visible.origin.y + 8:
            bottom = flipped_y + 24
        left = max(visible.origin.x + 8, left)
        bottom = min(
            max(visible.origin.y + 8, bottom), visible.origin.y + visible.size.height - height - 8
        )
        self.window.setFrame_display_(NSMakeRect(left, bottom, self.WIDTH, height), True)

    def show_at(self, x: float, y: float) -> None:
        if self.controller.quitting:
            return
        self.point = (x, y)
        self.height = float(self.HEIGHT)
        self.show()
        self.send(f"window.BridgeExplain && window.BridgeExplain.start({x:.1f}, {y:.1f})")

    def hide(self) -> None:
        super().hide()
        if self.overlay is not None:
            self.overlay.clear()

    def on_message(self, message: dict) -> None:
        if message.get("action") == "explain_size":
            try:
                self.height = min(max(float(message.get("height", self.HEIGHT)), 140.0), 480.0)
            except (TypeError, ValueError):
                return
            self.place()
            return
        if message.get("action") == "highlight":
            from app.desktop.guide_overlay import GuideOverlay

            if self.overlay is None:
                self.overlay = GuideOverlay()
            frame = message.get("frame")
            point = message.get("point")
            step = {
                "frame": [float(v) for v in frame][:4] if isinstance(frame, list) else None,
                "points": [[float(point[0]), float(point[1])]] if isinstance(point, list) else [],
                "text": "",
            }
            self.overlay.show(step)
            return
        super().on_message(message)


class BuddyTimer(NSObject):
    def tick_(self, _timer):
        self.owner.tick()


class DeskBuddy(FloatingPage):
    """Bridge's avatar in a corner of the screen."""

    PAGE = "buddy.html"
    WIDTH, HEIGHT = 96, 96

    def __init__(self, controller, service):
        super().__init__(controller, service)
        keep_private(self.window)
        self.window.setLevel_(AK.NSFloatingWindowLevel)
        self.window.setHasShadow_(False)
        self.window.setCollectionBehavior_(
            AK.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AK.NSWindowCollectionBehaviorStationary
            | AK.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AK.NSWindowCollectionBehaviorIgnoresCycle
        )
        content = self.window.contentView()
        content.layer().setCornerRadius_(0)
        self.size = (float(self.WIDTH), float(self.HEIGHT))
        self.corner = self._saved_corner()
        self.timer_target = BuddyTimer.alloc().init()
        self.timer_target.owner = self
        self.timer = AK.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            2.0, self.timer_target, b"tick:", None, True
        )

    # Where it sits -------------------------------------------------------------------------

    def _saved_corner(self) -> tuple[float, float]:
        visible = AK.NSScreen.mainScreen().visibleFrame()
        default = (visible.origin.x + visible.size.width - 18, visible.origin.y + 18)
        saved = NSUserDefaults.standardUserDefaults().arrayForKey_(POSITION_KEY)
        if saved and len(saved) == 2:
            x, y = float(saved[0]), float(saved[1])
            if self._on_a_screen(x - 40, y + 40):
                return x, y
        return default

    @staticmethod
    def _on_a_screen(x: float, y: float) -> bool:
        for screen in AK.NSScreen.screens():
            frame = screen.visibleFrame()
            if (
                frame.origin.x <= x <= frame.origin.x + frame.size.width
                and frame.origin.y <= y <= frame.origin.y + frame.size.height
            ):
                return True
        return False

    def place(self) -> None:
        width, height = self.size
        right, bottom = self.corner
        self.window.setFrame_display_(NSMakeRect(right - width, bottom, width, height), True)

    # Showing and hiding ------------------------------------------------------------------------

    def wanted(self) -> bool:
        settings = getattr(self.controller, "settings", None)
        if settings is None or not getattr(settings, "desk_buddy", True):
            return False
        if self.controller.quitting:
            return False
        # Out of the way while Bridge itself uses the screen.
        for name in ("operator_panel", "guide_panel"):
            panel = getattr(self.controller, name, None)
            if panel is not None and panel.window.isVisible():
                return False
        status = self.service.status
        return bool(status.url)

    def tick(self) -> None:
        import time

        from app.desktop.floating import show_in_screenshots

        settings = getattr(self.controller, "settings", None)
        until = float(getattr(settings, "panels_in_screenshots_until", 0) or 0)
        show_in_screenshots(until > time.time())
        if self.wanted():
            if not self.window.isVisible():
                if not self.loaded:
                    self.load()
                self.place()
                self.window.orderFrontRegardless()
        elif self.window.isVisible():
            self.window.orderOut_(None)

    def resigned(self) -> None:
        pass  # Stays on the desk.

    def show(self) -> None:
        self.tick()

    # Messages from the page --------------------------------------------------------------------

    def on_message(self, message: dict) -> None:
        action = message.get("action")
        if action == "buddy_size":
            try:
                width = min(max(float(message.get("width", 96)), 60), 420)
                height = min(max(float(message.get("height", 96)), 60), 420)
            except (TypeError, ValueError):
                return
            self.size = (width, height)
            self.place()
        elif action == "buddy_drag":
            try:
                dx, dy = float(message.get("dx", 0)), float(message.get("dy", 0))
            except (TypeError, ValueError):
                return
            right, bottom = self.corner
            self.corner = (right + dx, bottom - dy)  # The page's y grows downward.
            self.place()
        elif action == "buddy_dropped":
            NSUserDefaults.standardUserDefaults().setObject_forKey_(
                [self.corner[0], self.corner[1]], POSITION_KEY
            )
        elif action == "buddy_talk":
            if self.controller.talking():
                self.controller.talk.start()
            else:
                self.controller.command_bar.show()
        elif action == "buddy_ask":
            self.controller.command_bar.show()
        elif action == "open":
            view = str(message.get("view") or "")
            window = getattr(self.controller, "window", None)
            if window is not None:
                window.show(view if view.isalpha() else None)
            else:
                self.controller.open_dashboard()
        elif action == "hide":
            self.window.orderOut_(None)
        else:
            super().on_message(message)
