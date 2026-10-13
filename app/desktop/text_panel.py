"""The "act on selected text" panel (⌃⌥Space), drawn like the rest of Bridge (text.html).

A non-activating panel, like Spotlight: the app you were typing in stays active, so Replace
can paste the result straight back into it. The page does the asking (Improve, Translate…
through /api/v1/text/transform); this side opens it next to the pointer, pastes, copies,
hands requests to Bridge and opens the Accessibility settings. AppKit, main thread.
"""

from __future__ import annotations

import json

import AppKit as AK
from Foundation import NSMakeRect
from PyObjCTools import AppHelper

from app.desktop.floating import FloatingPage
from app.desktop.selection import (
    ACCESSIBILITY_SETTINGS,
    accessibility_allowed,
    copy_text,
    paste_text,
)


class TextPanel(FloatingPage):
    PAGE = "text.html"
    WIDTH, HEIGHT = 460, 480

    def __init__(self, controller, service, settings):
        super().__init__(controller, service)
        self.settings = settings
        self.window.setLevel_(AK.NSFloatingWindowLevel)
        self.pending: dict | None = None  # What to show once the page is ready.
        self.ready = False
        self.height = float(self.HEIGHT)

    def load(self, extra: str = "") -> bool:
        started = super().load(extra)
        if started:
            self.ready = False  # A fresh page: it says when it's ready.
        return started

    # Showing ----------------------------------------------------------------------------

    def place(self) -> None:
        mouse = AK.NSEvent.mouseLocation()
        screen = next(
            (s for s in AK.NSScreen.screens() if AK.NSMouseInRect(mouse, s.frame(), False)),
            AK.NSScreen.mainScreen(),
        ).visibleFrame()
        x = min(
            max(mouse.x - self.WIDTH / 2, screen.origin.x + 8),
            screen.origin.x + screen.size.width - self.WIDTH - 8,
        )
        y = min(
            max(mouse.y - self.height - 16, screen.origin.y + 8),
            screen.origin.y + screen.size.height - self.height - 8,
        )
        self.window.setFrame_display_(NSMakeRect(x, y, self.WIDTH, self.height), True)

    def _deliver(self, payload: dict) -> None:
        self.pending = payload
        if self.ready:
            self.send(f"window.BridgeText && window.BridgeText.show({json.dumps(payload)})")

    def open(self, text: str, app_name: str, allowed: bool) -> None:
        self.show()
        self._deliver({"mode": "selection", "text": text, "app": app_name, "allowed": allowed})

    def open_window(self, app_name: str) -> None:
        self.show()
        self._deliver(
            {"mode": "window", "text": "", "app": app_name, "allowed": True, "reading": True}
        )

    def fill_window(self, page: dict) -> None:
        if not self.window.isVisible():
            return
        self._deliver(
            {
                "mode": "window",
                "text": page.get("text", ""),
                "app": "",
                "title": page.get("window") or "",
                "allowed": True,
                "page": {k: page.get(k) for k in ("url", "file", "window")},
            }
        )

    def window_failed(self, message: str) -> None:
        if self.window.isVisible():
            self.send(f"window.BridgeText && window.BridgeText.failed({json.dumps(message)})")

    def close(self) -> None:
        self.hide()

    def resigned(self) -> None:
        pass  # Stays open while you look something up; Esc or Close hides it.

    # Messages from the page -----------------------------------------------------------------

    def on_message(self, message: dict) -> None:
        action = message.get("action")
        if action == "text_ready":
            self.ready = True
            if self.pending is not None:
                self._deliver(self.pending)
        elif action == "text_size":
            try:
                self.height = min(max(float(message.get("height", self.HEIGHT)), 240.0), 640.0)
            except (TypeError, ValueError):
                return
            frame = self.window.frame()
            top = frame.origin.y + frame.size.height  # Keep the top edge where it is.
            self.window.setFrame_display_(
                NSMakeRect(frame.origin.x, top - self.height, self.WIDTH, self.height), True
            )
        elif action == "text_replace":
            result = str(message.get("text") or "")
            if result.strip():
                self.hide()
                # Give the original app its keyboard focus back before pasting into it.
                AppHelper.callLater(0.15, paste_text, result)
        elif action == "text_copy":
            copy_text(str(message.get("text") or ""))
        elif action == "text_ask":
            request = str(message.get("request") or "")[:12000]
            if request and self.controller.submit_text(request):
                self.hide()
                self.controller.show_panel()
            else:
                self.send(
                    "window.BridgeText && window.BridgeText.failed("
                    "'Bridge is busy. Try again in a moment.')"
                )
        elif action == "text_allow":
            accessibility_allowed(prompt=True)
            AK.NSWorkspace.sharedWorkspace().openURL_(
                AK.NSURL.URLWithString_(ACCESSIBILITY_SETTINGS)
            )
        else:
            super().on_message(message)
