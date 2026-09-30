"""Small AppKit drawing components for Bridge's native voice panel.

No webview, graphics dependencies, or downloaded assets. Controls remain real NSButtons.
"""

from __future__ import annotations

import math
from functools import lru_cache

import AppKit as AK
import objc
from Foundation import NSMakePoint, NSMakeRect, NSString, NSZeroRect

TEXT = "F2F6FF"
MUTED = "A7B7D3"
FAINT = "7C91B3"
CYAN = "49DDFF"
GREEN = "49E4AE"
AMBER = "FFC67B"


def color(value: str, alpha: float = 1.0):
    return AK.NSColor.colorWithSRGBRed_green_blue_alpha_(
        int(value[0:2], 16) / 255,
        int(value[2:4], 16) / 255,
        int(value[4:6], 16) / 255,
        alpha,
    )


def rounded(rect, radius=16):
    return AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius)


def gradient(path, start, end, angle=25):
    AK.NSGradient.alloc().initWithStartingColor_endingColor_(start, end).drawInBezierPath_angle_(
        path, angle
    )


@lru_cache(maxsize=96)
def symbol(name: str, tint: str = TEXT):
    image = AK.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if image is not None:
        config = AK.NSImageSymbolConfiguration.configurationWithPaletteColors_([color(tint)])
        image = image.imageWithSymbolConfiguration_(config)
    return image


def draw_symbol(name, rect, tint=TEXT, opacity=1.0):
    image = symbol(name, tint)
    if image is not None:
        image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
            rect, NSZeroRect, AK.NSCompositingOperationSourceOver, opacity, True, None
        )


class PanelCanvas(AK.NSView):
    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        bounds = self.bounds()
        gradient(rounded(bounds, 0), color("0C1423"), color("111D32"), 65)
        # A restrained blue atmosphere behind the header/hero. Opaque base remains legible
        # with Reduce Transparency, regardless of the desktop behind the window.
        AK.NSGraphicsContext.saveGraphicsState()
        AK.NSBezierPath.bezierPathWithRect_(bounds).addClip()
        glow = AK.NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(bounds.size.width * 0.2, -220, 760, 600)
        )
        AK.NSGradient.alloc().initWithStartingColor_endingColor_(
            color("185CEC", 0.22), color("185CEC", 0)
        ).drawInBezierPath_relativeCenterPosition_(glow, NSMakePoint(0, 0))
        AK.NSGraphicsContext.restoreGraphicsState()


class Surface(AK.NSView):
    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        kind = getattr(self, "kind", "card")
        bounds = self.bounds()
        path = rounded(NSMakeRect(0.5, 0.5, bounds.size.width - 1, bounds.size.height - 1), 18)
        if kind == "hero":
            gradient(path, color("143A68"), color("181E3B"), 20)
            edge = "3264A5"
        else:
            gradient(path, color("19283C"), color("142033"), 25)
            edge = "2E425D"
        color(edge, 0.9).setStroke()
        path.setLineWidth_(1)
        path.stroke()


def draw_brand_mark():
    """Draw the Bridge monogram in a flipped 64×64 space; callers scale it for icons."""
    tile = rounded(NSMakeRect(1, 1, 62, 62), 17)
    gradient(tile, color("124577"), color("151A40"), 55)
    color("4B79C0", 0.7).setStroke()
    tile.setLineWidth_(1)
    tile.stroke()
    mark = AK.NSBezierPath.bezierPath()
    mark.moveToPoint_(NSMakePoint(18, 12))
    mark.lineToPoint_(NSMakePoint(34, 12))
    mark.curveToPoint_controlPoint1_controlPoint2_(
        NSMakePoint(49, 25), NSMakePoint(45, 12), NSMakePoint(49, 17)
    )
    mark.curveToPoint_controlPoint1_controlPoint2_(
        NSMakePoint(42, 33), NSMakePoint(49, 30), NSMakePoint(46, 32)
    )
    mark.curveToPoint_controlPoint1_controlPoint2_(
        NSMakePoint(51, 44), NSMakePoint(49, 35), NSMakePoint(51, 39)
    )
    mark.curveToPoint_controlPoint1_controlPoint2_(
        NSMakePoint(36, 55), NSMakePoint(51, 52), NSMakePoint(45, 55)
    )
    mark.lineToPoint_(NSMakePoint(18, 55))
    mark.closePath()
    mark.appendBezierPath_(rounded(NSMakeRect(28, 21, 11, 8), 4))
    mark.appendBezierPath_(rounded(NSMakeRect(28, 38, 13, 8), 4))
    mark.setWindingRule_(AK.NSEvenOddWindingRule)
    gradient(mark, color("13E4EE"), color("A33EFF"), 45)
    bridge = AK.NSBezierPath.bezierPath()
    bridge.moveToPoint_(NSMakePoint(10, 53))
    bridge.curveToPoint_controlPoint1_controlPoint2_(
        NSMakePoint(42, 27), NSMakePoint(18, 38), NSMakePoint(32, 23)
    )
    bridge.setLineWidth_(5)
    bridge.setLineCapStyle_(AK.NSRoundLineCapStyle)
    color("31CFFF").setStroke()
    bridge.stroke()


class BrandMark(AK.NSView):
    """Resolution-independent Bridge monogram, matching the panel's blue/violet palette."""

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        draw_brand_mark()


class VoiceOrb(AK.NSView):
    """State animation, not a fabricated microphone level meter."""

    def isFlipped(self):
        return True

    @objc.python_method
    def update(self, phase: str, tick: float = 0.0):
        self.phase = phase
        self.tick = tick
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        width, height = self.bounds().size
        cx, cy = width / 2, height / 2
        tick = getattr(self, "tick", 0.0)
        phase = getattr(self, "phase", "stopped")
        active = phase in {"listening", "recording", "transcribing", "processing", "speaking"}
        pulse = (math.sin(tick * 2.4) + 1) / 2 if active else 0.3
        for radius, alpha in [(80, 0.12), (69, 0.22), (58, 0.38)]:
            ring = AK.NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - radius, cy - radius, radius * 2, radius * 2)
            )
            color(CYAN, alpha + pulse * 0.08).setStroke()
            ring.setLineWidth_(1)
            ring.stroke()
        glow = AK.NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - 64, cy - 64, 128, 128))
        AK.NSGradient.alloc().initWithStartingColor_endingColor_(
            color("188FFF", 0.75), color("188FFF", 0)
        ).drawInBezierPath_relativeCenterPosition_(glow, NSMakePoint(0, 0))
        orb = AK.NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - 49, cy - 49, 98, 98))
        gradient(orb, color("04D9F8"), color("702AF5"), 40)
        color("A1E9FF", 0.85).setStroke()
        orb.setLineWidth_(1.3)
        orb.stroke()
        if phase in {"recording", "speaking", "processing", "transcribing"}:
            for index in range(5):
                wave = math.sin(tick * 3 + index * 0.9) if active else 0
                bar_height = 13 + (1 - abs(index - 2) / 3) * 22 + wave * 6
                bar = rounded(
                    NSMakeRect(cx - 23 + index * 10, cy - bar_height / 2, 5, bar_height), 3
                )
                color(TEXT).setFill()
                bar.fill()
        else:
            draw_symbol("mic.fill", NSMakeRect(cx - 17, cy - 24, 34, 48))


class StyledButton(AK.NSButton):
    """Native keyboard/action/accessibility semantics with custom drawing."""

    def isFlipped(self):
        return True

    def updateTrackingAreas(self):
        previous = getattr(self, "tracking", None)
        if previous is not None:
            self.removeTrackingArea_(previous)
        self.tracking = AK.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            AK.NSTrackingMouseEnteredAndExited
            | AK.NSTrackingActiveInKeyWindow
            | AK.NSTrackingInVisibleRect,
            self,
            None,
        )
        self.addTrackingArea_(self.tracking)
        objc.super(StyledButton, self).updateTrackingAreas()

    def mouseEntered_(self, event):
        self.hovered = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        self.hovered = False
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        width, height = self.bounds().size
        style = getattr(self, "style", "secondary")
        enabled = self.isEnabled()
        hovered = getattr(self, "hovered", False) and enabled
        pressed = self.cell().isHighlighted() and enabled
        opacity = 1.0 if enabled else 0.42
        path = rounded(NSMakeRect(0.5, 0.5, width - 1, height - 1), 12)
        if style == "primary":
            gradient(
                path,
                color("07BBEE" if hovered else "079FEF", opacity),
                color("1557EF" if pressed else "236CF5", opacity),
                10,
            )
            edge = "67DCFF"
        else:
            gradient(
                path,
                color("2A405F" if hovered else "20324D", opacity),
                color("1C2A44" if not pressed else "132038", opacity),
                35,
            )
            edge = "5B759B" if hovered else "354C6D"
        color(edge, opacity * 0.8).setStroke()
        path.setLineWidth_(1)
        path.stroke()
        if self.window() is not None and self.window().firstResponder() == self:
            color(CYAN).setStroke()
            path.setLineWidth_(2)
            path.stroke()
        icon = getattr(self, "icon", None)
        title = self.title()
        font_size = getattr(self, "font_size", 14)
        attrs = {
            AK.NSFontAttributeName: AK.NSFont.systemFontOfSize_weight_(
                font_size, AK.NSFontWeightMedium
            ),
            AK.NSForegroundColorAttributeName: color(TEXT, opacity),
        }
        text = NSString.stringWithString_(title)
        text_width = text.sizeWithAttributes_(attrs).width
        subtitle = getattr(self, "subtitle", "")
        if style == "tile":
            draw_symbol(
                icon or "sparkles", NSMakeRect(15, 14, 22, 22), getattr(self, "tint", CYAN), opacity
            )
            text.drawAtPoint_withAttributes_(NSMakePoint(15, 45), attrs)
            sub_attrs = {
                AK.NSFontAttributeName: AK.NSFont.systemFontOfSize_(10),
                AK.NSForegroundColorAttributeName: color(MUTED, opacity),
            }
            NSString.stringWithString_(subtitle).drawAtPoint_withAttributes_(
                NSMakePoint(15, 65), sub_attrs
            )
            draw_symbol("chevron.right", NSMakeRect(width - 24, 18, 8, 12), MUTED, opacity)
        elif style == "icon":
            size = min(19, height - 9)
            draw_symbol(
                icon or "gearshape",
                NSMakeRect((width - size) / 2, (height - size) / 2, size, size),
                getattr(self, "icon_tint", MUTED),
                opacity,
            )
        elif getattr(self, "title_hidden", False):
            draw_symbol(
                icon, NSMakeRect((width - 18) / 2, (height - 18) / 2, 18, 18), TEXT, opacity
            )
        else:
            total = text_width + (28 if icon else 0)
            start = max(12, (width - total) / 2)
            if icon:
                draw_symbol(icon, NSMakeRect(start, (height - 18) / 2, 18, 18), TEXT, opacity)
                start += 28
            text.drawAtPoint_withAttributes_(
                NSMakePoint(start, (height - font_size * 1.25) / 2), attrs
            )
