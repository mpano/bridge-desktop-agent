"""Bridge's hands on the Mac: screenshots, what's under a click, and mouse and keyboard.

Used by the operator (app/assistant/operator.py) to carry out a computer-use model's steps.
Every step is checked first: macOS's accessibility information says which app, button or
field an action lands on, so the operator can ask before "Send" and refuse password fields.
Needs Screen Recording (to see) and Accessibility (to read the UI and to act). Coordinates are
Quartz global points, top-left origin, on the main display.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

import Quartz
from AppKit import NSWorkspace
from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    AXUIElementCopyElementAtPosition,
    AXUIElementCreateSystemWide,
    AXUIElementGetPid,
)

SHOT_WIDTH = 1440  # What the model sees; actions are mapped back to the real screen.

KEYS = {
    "a": 0x00,
    "s": 0x01,
    "d": 0x02,
    "f": 0x03,
    "h": 0x04,
    "g": 0x05,
    "z": 0x06,
    "x": 0x07,
    "c": 0x08,
    "v": 0x09,
    "b": 0x0B,
    "q": 0x0C,
    "w": 0x0D,
    "e": 0x0E,
    "r": 0x0F,
    "y": 0x10,
    "t": 0x11,
    "1": 0x12,
    "2": 0x13,
    "3": 0x14,
    "4": 0x15,
    "6": 0x16,
    "5": 0x17,
    "=": 0x18,
    "9": 0x19,
    "7": 0x1A,
    "-": 0x1B,
    "8": 0x1C,
    "0": 0x1D,
    "]": 0x1E,
    "o": 0x1F,
    "u": 0x20,
    "[": 0x21,
    "i": 0x22,
    "p": 0x23,
    "l": 0x25,
    "j": 0x26,
    "'": 0x27,
    "k": 0x28,
    ";": 0x29,
    "\\": 0x2A,
    ",": 0x2B,
    "/": 0x2C,
    "n": 0x2D,
    "m": 0x2E,
    ".": 0x2F,
    "`": 0x32,
    "enter": 0x24,
    "return": 0x24,
    "tab": 0x30,
    "space": 0x31,
    "backspace": 0x33,
    "esc": 0x35,
    "escape": 0x35,
    "delete": 0x75,
    "del": 0x75,
    "home": 0x73,
    "end": 0x77,
    "pageup": 0x74,
    "pagedown": 0x79,
    "arrowleft": 0x7B,
    "left": 0x7B,
    "arrowright": 0x7C,
    "right": 0x7C,
    "arrowdown": 0x7D,
    "down": 0x7D,
    "arrowup": 0x7E,
    "up": 0x7E,
    "f1": 0x7A,
    "f2": 0x78,
    "f3": 0x63,
    "f4": 0x76,
    "f5": 0x60,
    "f6": 0x61,
    "f7": 0x62,
    "f8": 0x64,
    "f9": 0x65,
    "f10": 0x6D,
    "f11": 0x67,
    "f12": 0x6F,
}
MODIFIERS = {
    "cmd": Quartz.kCGEventFlagMaskCommand,
    "command": Quartz.kCGEventFlagMaskCommand,
    "meta": Quartz.kCGEventFlagMaskCommand,
    "super": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "ctrl": Quartz.kCGEventFlagMaskControl,
    "control": Quartz.kCGEventFlagMaskControl,
    "alt": Quartz.kCGEventFlagMaskAlternate,
    "option": Quartz.kCGEventFlagMaskAlternate,
}
BUTTONS = {
    "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft),
    "right": (
        Quartz.kCGEventRightMouseDown,
        Quartz.kCGEventRightMouseUp,
        Quartz.kCGMouseButtonRight,
    ),
    "wheel": (
        Quartz.kCGEventOtherMouseDown,
        Quartz.kCGEventOtherMouseUp,
        Quartz.kCGMouseButtonCenter,
    ),
}


def _ax(element, attribute: str):
    error, value = AXUIElementCopyAttributeValue(element, attribute, None)
    return value if error == 0 else None


def _text(value) -> str:
    return " ".join(str(value).split())[:120] if value not in (None, "") else ""


def _flags(keys) -> int:
    flags = 0
    for key in keys or []:
        flags |= MODIFIERS.get(str(key).lower(), 0)
    return flags


class MacHands:
    """The real Mac. Methods run on any thread; nothing here touches AppKit windows."""

    def __init__(self):
        self.system = AXUIElementCreateSystemWide()
        self.last_point: tuple[float, float] | None = None

    # Seeing --------------------------------------------------------------------------------

    def screen_size(self) -> tuple[float, float]:
        bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return float(bounds.size.width), float(bounds.size.height)

    def screenshot(self) -> tuple[bytes, int, int]:
        """The main display as a JPEG about SHOT_WIDTH wide (deleted from disk at once)."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "screen.png"
            subprocess.run(
                ["/usr/sbin/screencapture", "-x", "-m", "-t", "png", str(path)],
                check=False,
                timeout=10,
                capture_output=True,
            )
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError("Allow Screen Recording for Bridge so it can see the screen.")
            data = path.read_bytes()
        source = Quartz.CGImageSourceCreateWithData(data, None)
        image = Quartz.CGImageSourceCreateThumbnailAtIndex(
            source,
            0,
            {
                Quartz.kCGImageSourceCreateThumbnailFromImageAlways: True,
                Quartz.kCGImageSourceThumbnailMaxPixelSize: SHOT_WIDTH,
                Quartz.kCGImageSourceCreateThumbnailWithTransform: True,
            },
        )
        out = Quartz.CFDataCreateMutable(None, 0)
        destination = Quartz.CGImageDestinationCreateWithData(out, "public.jpeg", 1, None)
        Quartz.CGImageDestinationAddImage(
            destination, image, {Quartz.kCGImageDestinationLossyCompressionQuality: 0.82}
        )
        Quartz.CGImageDestinationFinalize(destination)
        return bytes(out), Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)

    def frontmost(self) -> dict:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return {"app": "", "bundle_id": ""}
        return {
            "app": str(app.localizedName() or ""),
            "bundle_id": str(app.bundleIdentifier() or ""),
        }

    def _app_of(self, element) -> dict:
        error, pid = AXUIElementGetPid(element, None)
        if error != 0:
            return {"app": "", "bundle_id": ""}
        from AppKit import NSRunningApplication

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            return {"app": "", "bundle_id": ""}
        return {
            "app": str(app.localizedName() or ""),
            "bundle_id": str(app.bundleIdentifier() or ""),
        }

    def element_at(self, x: float, y: float) -> dict:
        """What a click at (x, y) would hit: its role, label, and app."""
        error, element = AXUIElementCopyElementAtPosition(self.system, x, y, None)
        if error != 0 or element is None:
            return {"role": "", "label": "", "app": "", "bundle_id": ""}
        role = _text(_ax(element, "AXRole"))
        label = _text(_ax(element, "AXTitle")) or _text(_ax(element, "AXDescription"))
        # A label inside a button: report the button.
        parent = _ax(element, "AXParent")
        if role in {"AXStaticText", "AXImage"} and parent is not None:
            parent_role = _text(_ax(parent, "AXRole"))
            if parent_role in {"AXButton", "AXMenuItem", "AXLink", "AXMenuButton"}:
                role = parent_role
                label = (
                    _text(_ax(parent, "AXTitle")) or _text(_ax(parent, "AXDescription")) or label
                )
        if not label and role in {"AXStaticText"}:
            label = _text(_ax(element, "AXValue"))
        return {
            "role": role,
            "subrole": _text(_ax(element, "AXSubrole")),
            "label": label,
            **self._app_of(element),
        }

    def focused(self) -> dict:
        """The field that would receive typing."""
        element = _ax(self.system, "AXFocusedUIElement")
        if element is None:
            return {"role": "", "subrole": "", "label": "", **self.frontmost()}
        return {
            "role": _text(_ax(element, "AXRole")),
            "subrole": _text(_ax(element, "AXSubrole")),
            "label": _text(_ax(element, "AXTitle")) or _text(_ax(element, "AXDescription")),
            **self._app_of(element),
        }

    def mouse(self) -> tuple[float, float]:
        point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return float(point.x), float(point.y)

    # Acting --------------------------------------------------------------------------------

    def _post(self, event, flags: int = 0) -> None:
        if flags:
            Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def move(self, x: float, y: float, keys=()) -> None:
        event = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft
        )
        self._post(event, _flags(keys))
        self.last_point = (x, y)

    def click(self, x: float, y: float, button: str = "left", count: int = 1, keys=()) -> None:
        down, up, which = BUTTONS.get(button, BUTTONS["left"])
        flags = _flags(keys)
        self.move(x, y)
        time.sleep(0.05)
        for n in range(1, count + 1):
            for kind in (down, up):
                event = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), which)
                Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, n)
                self._post(event, flags)
                time.sleep(0.02)
        self.last_point = (x, y)

    def drag(self, path: list[tuple[float, float]], keys=()) -> None:
        flags = _flags(keys)
        start = path[0]
        self.move(*start)
        self._post(
            Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseDown, start, Quartz.kCGMouseButtonLeft
            ),
            flags,
        )
        for point in path[1:]:
            time.sleep(0.03)
            self._post(
                Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventLeftMouseDragged, point, Quartz.kCGMouseButtonLeft
                ),
                flags,
            )
        self._post(
            Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseUp, path[-1], Quartz.kCGMouseButtonLeft
            ),
            flags,
        )
        self.last_point = path[-1]

    def scroll(self, x: float, y: float, dx: float, dy: float, keys=()) -> None:
        self.move(x, y)
        event = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitPixel, 2, int(-dy), int(-dx)
        )
        self._post(event, _flags(keys))

    def type(self, text: str) -> None:
        for line_number, line in enumerate(text.split("\n")):
            if line_number:
                self.keypress(["enter"])
            for start in range(0, len(line), 20):
                chunk = line[start : start + 20]
                for down in (True, False):
                    event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
                    Quartz.CGEventKeyboardSetUnicodeString(event, len(chunk), chunk)
                    self._post(event)
                time.sleep(0.01)

    def keypress(self, keys: list[str]) -> None:
        names = [str(k).lower() for k in keys]
        flags = _flags(names)
        main = [k for k in names if k not in MODIFIERS] or names[-1:]
        for key in main:
            code = KEYS.get(key)
            if code is None:
                raise ValueError(f"Bridge doesn't know the key {key}.")
            for down in (True, False):
                self._post(Quartz.CGEventCreateKeyboardEvent(None, code, down), flags)
            time.sleep(0.02)
