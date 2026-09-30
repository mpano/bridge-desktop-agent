"""Read the text selected in the frontmost app, and paste text back into it.

Both need Accessibility permission for Bridge (System Settings > Privacy & Security >
Accessibility). The clipboard is always restored after a copy or paste.
"""

from __future__ import annotations

import time

import AppKit as AK

ACCESSIBILITY_SETTINGS = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)
KEY_C, KEY_V = 8, 9


def accessibility_allowed(prompt: bool = False) -> bool:
    import ApplicationServices as AS

    return bool(AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: prompt}))


def _selected_via_accessibility() -> str:
    import ApplicationServices as AS

    system = AS.AXUIElementCreateSystemWide()
    error, focused = AS.AXUIElementCopyAttributeValue(system, AS.kAXFocusedUIElementAttribute, None)
    if error or focused is None:
        return ""
    error, text = AS.AXUIElementCopyAttributeValue(focused, AS.kAXSelectedTextAttribute, None)
    return str(text) if not error and text else ""


def _press(key: int) -> None:
    import Quartz as Q

    for down in (True, False):
        event = Q.CGEventCreateKeyboardEvent(None, key, down)
        Q.CGEventSetFlags(event, Q.kCGEventFlagMaskCommand)
        Q.CGEventPost(Q.kCGHIDEventTap, event)


class ClipboardSnapshot:
    """Every item and type on the general pasteboard, so it can be put back exactly."""

    def __init__(self):
        self.board = AK.NSPasteboard.generalPasteboard()
        self.items = []
        for item in self.board.pasteboardItems() or []:
            copy = {}
            for kind in item.types():
                data = item.dataForType_(kind)
                if data is not None:
                    copy[kind] = data
            self.items.append(copy)

    def restore(self) -> None:
        self.board.clearContents()
        restored = []
        for copy in self.items:
            item = AK.NSPasteboardItem.alloc().init()
            for kind, data in copy.items():
                item.setData_forType_(data, kind)
            restored.append(item)
        if restored:
            self.board.writeObjects_(restored)


def read_selection(timeout: float = 0.35) -> str:
    """The selection in the frontmost app, or "" when nothing is selected."""
    text = _selected_via_accessibility()
    if text.strip():
        return text
    # Browsers and Electron apps often hide the selection from Accessibility: copy it.
    snapshot = ClipboardSnapshot()
    board = snapshot.board
    before = board.changeCount()
    _press(KEY_C)
    deadline = time.monotonic() + timeout
    while board.changeCount() == before and time.monotonic() < deadline:
        time.sleep(0.02)
    copied = (
        board.stringForType_(AK.NSPasteboardTypeString) if board.changeCount() != before else None
    )
    snapshot.restore()
    return str(copied or "")


def paste_text(text: str, settle: float = 0.35) -> None:
    """Replace the selection in the frontmost app with text, then restore the clipboard."""
    snapshot = ClipboardSnapshot()
    board = snapshot.board
    board.clearContents()
    board.setString_forType_(text, AK.NSPasteboardTypeString)
    _press(KEY_V)
    time.sleep(settle)  # Let the app read the clipboard before it's restored.
    snapshot.restore()


def copy_text(text: str) -> None:
    board = AK.NSPasteboard.generalPasteboard()
    board.clearContents()
    board.setString_forType_(text, AK.NSPasteboardTypeString)
