"""The menu bar icon: the bridge as a template image, with Bridge's state on it.

Ready is a plain template, so macOS tints it for light and dark menu bars. The other
states add a colored dot; they draw the bridge in the label color of the menu bar they
are drawn in, so they stay right in both appearances.
"""

from __future__ import annotations

from functools import lru_cache

import AppKit as AK
from Foundation import NSMakePoint, NSMakeRect

READY, NEEDS_YOU, LISTENING = "ready", "needs_you", "listening"
DOTS = {NEEDS_YOU: (0xF0, 0x8A, 0x24), LISTENING: (0xFF, 0x5D, 0x5D)}
SIZE = (18, 18)


def _stroke(path, ink) -> None:
    path.setLineWidth_(1.6)
    path.setLineCapStyle_(AK.NSRoundLineCapStyle)
    ink.setStroke()
    path.stroke()


def draw_glyph(ink) -> None:
    """The bridge in an 18 pt flipped square: cable, two towers and the deck."""
    cable = AK.NSBezierPath.bezierPath()
    cable.moveToPoint_(NSMakePoint(2, 10))
    cable.curveToPoint_controlPoint1_controlPoint2_(
        NSMakePoint(16, 10), NSMakePoint(5, 4), NSMakePoint(13, 4)
    )
    _stroke(cable, ink)
    for start, end in (((5.5, 7.3), (5.5, 14)), ((12.5, 7.3), (12.5, 14)), ((1.5, 12), (16.5, 12))):
        line = AK.NSBezierPath.bezierPath()
        line.moveToPoint_(NSMakePoint(*start))
        line.lineToPoint_(NSMakePoint(*end))
        _stroke(line, ink)


@lru_cache(maxsize=4)
def status_image(state: str = READY):
    dot = DOTS.get(state)

    def draw(_rect):
        draw_glyph(AK.NSColor.blackColor() if dot is None else AK.NSColor.labelColor())
        if dot is not None:
            red, green, blue = (value / 255 for value in dot)
            AK.NSColor.colorWithSRGBRed_green_blue_alpha_(red, green, blue, 1).setFill()
            AK.NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(12.6, 0.6, 5, 5)).fill()
        return True

    image = AK.NSImage.imageWithSize_flipped_drawingHandler_(SIZE, True, draw)
    image.setTemplate_(dot is None)
    image.setAccessibilityDescription_(
        {READY: "Bridge", NEEDS_YOU: "Bridge — needs your OK", LISTENING: "Bridge — listening"}[
            state
        ]
    )
    return image
