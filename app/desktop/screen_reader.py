"""Read what the user is looking at: the frontmost window that isn't Bridge.

Opening Bridge's panel makes Bridge frontmost, so "this" means the window just behind it.
The window list (Quartz) gives that window without needing a run loop; its text comes
from the Accessibility tree, which Bridge already has permission for. Password fields are
never read.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

MAX_NODES = 4000
MAX_CHARS = 12000
TIME_BUDGET = 2.0
TEXT_ROLES = {"AXStaticText", "AXTextArea", "AXTextField", "AXHeading", "AXCell"}
SKIP_ROLES = {"AXSecureTextField", "AXMenuBar", "AXMenu", "AXScrollBar", "AXToolbar"}
CHROMIUM = (
    "com.google.Chrome",
    "com.brave.Browser",
    "com.microsoft.edgemac",
    "company.thebrowser.Browser",
    "com.vivaldi.Vivaldi",
    "org.chromium.Chromium",
)
SYSTEM_OWNERS = {"Window Server", "Dock", "Control Center", "Notification Center", "SystemUIServer"}


def front_windows() -> list[dict]:
    """On-screen app windows, front to back, excluding Bridge's own."""
    import Quartz as Q

    options = Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements
    windows = []
    for info in Q.CGWindowListCopyWindowInfo(options, Q.kCGNullWindowID) or []:
        bounds = info.get("kCGWindowBounds") or {}
        if (
            info.get("kCGWindowLayer", 0) != 0
            or info.get("kCGWindowOwnerPID") == os.getpid()
            or info.get("kCGWindowOwnerName") in SYSTEM_OWNERS
            or info.get("kCGWindowAlpha", 1) == 0
            or bounds.get("Height", 0) < 80
            or bounds.get("Width", 0) < 120
        ):
            continue
        windows.append(
            {
                "pid": int(info["kCGWindowOwnerPID"]),
                "owner": str(info.get("kCGWindowOwnerName") or ""),
                "number": int(info["kCGWindowNumber"]),
            }
        )
    return windows


def app_info(pid: int) -> dict:
    import AppKit as AK

    app = AK.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        return {"name": "", "bundle_id": ""}
    return {"name": str(app.localizedName() or ""), "bundle_id": str(app.bundleIdentifier() or "")}


def _get(element, attribute):
    import ApplicationServices as AS

    error, value = AS.AXUIElementCopyAttributeValue(element, attribute, None)
    return None if error else value


def _text(value) -> str:
    if value is None:
        return ""
    try:
        return " ".join(str(value).split())
    except Exception:
        return ""


def read_window(pid: int, bundle_id: str = "") -> dict:
    """Title, document, URL, selection and visible text of the app's focused window."""
    import ApplicationServices as AS

    app = AS.AXUIElementCreateApplication(pid)
    # Electron apps build their web Accessibility tree only when asked; Chromium browsers
    # need the flag screen readers set. It is switched back off after reading.
    AS.AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)
    enhanced = bundle_id.startswith(CHROMIUM) and not _get(app, "AXEnhancedUserInterface")
    if enhanced:
        AS.AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface", True)
    try:
        return _read(app)
    finally:
        if enhanced:
            AS.AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface", False)


def _read(app) -> dict:
    window = _get(app, "AXFocusedWindow") or _get(app, "AXMainWindow")
    if window is None:
        return {"window": "", "file": "", "url": "", "selection": "", "text": ""}
    focused = _get(app, "AXFocusedUIElement")
    selection = ""
    if focused is not None and _text(_get(focused, "AXRole")) != "AXSecureTextField":
        selection = str(_get(focused, "AXSelectedText") or "")
    found = _walk(window)
    deadline = time.monotonic() + 4  # Chrome needs ~2s to build a page the first time.
    while not found["text"] and time.monotonic() < deadline:
        time.sleep(0.25)  # The web tree is built in the background the first time.
        found = _walk(window)
    document = _text(_get(window, "AXDocument"))
    return {
        "window": _text(_get(window, "AXTitle")),
        "file": document[7:] if document.startswith("file://") else "",
        "url": found["url"] or (document if document.startswith("http") else ""),
        "selection": selection.strip()[:4000],
        "text": found["text"],
        "truncated": found["truncated"],
    }


def _walk(window) -> dict:
    lines, url, seen, total = [], "", 0, 0
    deadline = time.monotonic() + TIME_BUDGET
    stack = [window]
    truncated = False
    while stack:
        if seen >= MAX_NODES or total >= MAX_CHARS or time.monotonic() > deadline:
            truncated = True
            break
        element = stack.pop()
        seen += 1
        role = _text(_get(element, "AXRole"))
        if role in SKIP_ROLES:
            continue
        if role == "AXWebArea" and not url:
            url = _text(_get(element, "AXURL"))
        if role in TEXT_ROLES:
            value = _get(element, "AXValue")
            text = _text(value if isinstance(value, str) else _get(element, "AXTitle"))
            if text and (not lines or lines[-1] != text):
                lines.append(text)
                total += len(text) + 1
        children = _get(element, "AXChildren") or []
        stack.extend(reversed(list(children)))  # Depth-first, in reading order.
    text = "\n".join(lines)
    return {"text": text[:MAX_CHARS], "url": url, "truncated": truncated}


def window_title(pid: int) -> str:
    """Just the focused window's title (no content), without asking for permission."""
    import ApplicationServices as AS

    if not AS.AXIsProcessTrusted():
        return ""
    app = AS.AXUIElementCreateApplication(pid)
    window = _get(app, "AXFocusedWindow") or _get(app, "AXMainWindow")
    return _text(_get(window, "AXTitle")) if window is not None else ""


def screen_recording_allowed() -> bool:
    import Quartz as Q

    return bool(Q.CGPreflightScreenCaptureAccess())


def capture_window(number: int) -> bytes:
    """A PNG of one window, deleted from disk right away."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "window.png"
        subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-o", "-l", str(number), str(path)],
            check=False,
            timeout=10,
            capture_output=True,
        )
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Allow Screen Recording for Bridge to read this window.")
        return path.read_bytes()


def accessibility_allowed() -> bool:
    from app.desktop.selection import accessibility_allowed as allowed

    return allowed(prompt=True)  # Shows macOS's prompt the first time.


class MacScreenReader:
    """The macOS implementation used by the screen_context tool."""

    accessibility_allowed = staticmethod(accessibility_allowed)
    front_windows = staticmethod(front_windows)
    app_info = staticmethod(app_info)
    read_window = staticmethod(read_window)
    window_title = staticmethod(window_title)
    screen_recording_allowed = staticmethod(screen_recording_allowed)
    capture_window = staticmethod(capture_window)
