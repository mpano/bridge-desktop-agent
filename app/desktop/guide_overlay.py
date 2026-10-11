"""Show me how, on screen: a highlight on the exact spot to click, and the guide panel.

The overlay is a transparent window over the whole main display that never takes clicks (you
click straight through it onto the real button), floats above open menus, and is left out of
screenshots so the model never sees Bridge's own drawing. The guide panel (guide.html) sits at
the bottom of the screen with the step, Next, Do this step for me and Stop; it moves to the
top if the step is underneath it. AppKit work happens on the main thread.
"""

from __future__ import annotations

import threading
import time

import AppKit as AK
import Quartz
from Foundation import NSMakeRect

from app.desktop.floating import FloatingPage

ACCENT = (0.43, 0.49, 1.0)  # Bridge's violet-blue.
PAD = 6.0


def _color(alpha: float):
    return Quartz.CGColorCreateGenericRGB(*ACCENT, alpha)


class OverlayView(AK.NSView):
    def isFlipped(self):  # Top-left origin, the same as the Quartz points the guide uses.
        return True


class GuideOverlay:
    def __init__(self):
        screen = AK.NSScreen.mainScreen()
        self.window = AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            screen.frame(), AK.NSWindowStyleMaskBorderless, AK.NSBackingStoreBuffered, False
        )
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(AK.NSColor.clearColor())
        self.window.setHasShadow_(False)
        self.window.setIgnoresMouseEvents_(True)
        self.window.setLevel_(AK.NSPopUpMenuWindowLevel + 1)
        self.window.setSharingType_(AK.NSWindowSharingNone)
        self.window.setReleasedWhenClosed_(False)
        self.window.setCollectionBehavior_(
            AK.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AK.NSWindowCollectionBehaviorStationary
            | AK.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AK.NSWindowCollectionBehaviorIgnoresCycle
        )
        self.view = OverlayView.alloc().initWithFrame_(screen.frame())
        self.view.setWantsLayer_(True)
        self.window.setContentView_(self.view)

    def _ring(self, rect, round_: float):
        """A soft highlight with a pulse around it."""
        path = Quartz.CGPathCreateWithRoundedRect(rect, round_, round_, None)
        base = Quartz.CAShapeLayer.layer()
        base.setPath_(path)
        base.setFillColor_(_color(0.14))
        base.setStrokeColor_(_color(0.95))
        base.setLineWidth_(3)
        pulse = Quartz.CAShapeLayer.layer()
        pulse.setFrame_(rect)
        local = Quartz.CGPathCreateWithRoundedRect(
            Quartz.CGRectMake(0, 0, rect.size.width, rect.size.height), round_, round_, None
        )
        pulse.setPath_(local)
        pulse.setFillColor_(None)
        pulse.setStrokeColor_(_color(0.8))
        pulse.setLineWidth_(3)
        grow = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
        grow.setFromValue_(1.0)
        grow.setToValue_(1.35)
        fade = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
        fade.setFromValue_(0.9)
        fade.setToValue_(0.0)
        group = Quartz.CAAnimationGroup.animation()
        group.setAnimations_([grow, fade])
        group.setDuration_(1.3)
        group.setRepeatCount_(1e9)
        pulse.addAnimation_forKey_(group, "pulse")
        return [base, pulse]

    def _label(self, text: str, tip: str, near, screen_size):
        """The instruction next to the highlight: below it, or above near the bottom."""
        words = text.replace("**", "").replace("`", "“", 1).replace("`", "”", 1)
        shown = words + (f"\n{tip}" if tip else "")
        font = AK.NSFont.systemFontOfSize_weight_(15, AK.NSFontWeightSemibold)
        attributes = {AK.NSFontAttributeName: font}
        size = (
            AK.NSString.stringWithString_(shown)
            .boundingRectWithSize_options_attributes_(
                AK.NSMakeSize(380, 400),
                AK.NSStringDrawingUsesLineFragmentOrigin,
                attributes,
            )
            .size
        )
        width, height = min(400, size.width + 28), size.height + 18
        x = min(max(12, near[0] - width / 2), screen_size[0] - width - 12)
        y = near[1] + 14
        if y + height > screen_size[1] - 20:
            y = near[2] - height - 14
        box = Quartz.CALayer.layer()
        box.setFrame_(Quartz.CGRectMake(x, y, width, height))
        box.setBackgroundColor_(Quartz.CGColorCreateGenericRGB(0.11, 0.11, 0.14, 0.94))
        box.setCornerRadius_(10)
        box.setBorderColor_(_color(0.9))
        box.setBorderWidth_(1.5)
        box.setShadowOpacity_(0.35)
        box.setShadowRadius_(10)
        box.setShadowOffset_(Quartz.CGSizeMake(0, 4))
        label = Quartz.CATextLayer.layer()
        label.setFrame_(Quartz.CGRectMake(14, 9, width - 28, height - 18))
        label.setString_(shown)
        label.setFont_(font)
        label.setFontSize_(15)
        label.setForegroundColor_(Quartz.CGColorCreateGenericRGB(1, 1, 1, 1))
        label.setWrapped_(True)
        label.setContentsScale_(AK.NSScreen.mainScreen().backingScaleFactor())
        box.addSublayer_(label)
        return box

    def show(self, step: dict) -> None:
        layer = self.view.layer()
        layer.setSublayers_([])
        frame = self.view.frame().size
        screen = (frame.width, frame.height)
        layers = []
        points = step.get("points") or []
        rect = step.get("frame")
        if rect:
            box = Quartz.CGRectMake(
                rect[0] - PAD, rect[1] - PAD, rect[2] + 2 * PAD, rect[3] + 2 * PAD
            )
            layers += self._ring(box, 8)
            near = (rect[0] + rect[2] / 2, rect[1] + rect[3] + PAD, rect[1] - PAD)
        elif points:
            for x, y in points[:2]:
                layers += self._ring(Quartz.CGRectMake(x - 22, y - 22, 44, 44), 22)
            if len(points) > 1:
                line = Quartz.CAShapeLayer.layer()
                path = Quartz.CGPathCreateMutable()
                Quartz.CGPathMoveToPoint(path, None, *points[0])
                Quartz.CGPathAddLineToPoint(path, None, *points[-1])
                line.setPath_(path)
                line.setStrokeColor_(_color(0.9))
                line.setLineWidth_(3)
                line.setLineDashPattern_([8, 6])
                layers.append(line)
            x, y = points[0]
            near = (x, y + 22, y - 22)
        else:
            near = (screen[0] / 2, screen[1] * 0.18, screen[1] * 0.18)
        text = step.get("text", "")
        tip = step.get("tip") or ""
        if step.get("keys"):
            tip = tip or "Press the keys together"
        layers.append(self._label(f"{step.get('n', '')}. {text}", tip, near, screen))
        for item in layers:
            layer.addSublayer_(item)
        self.window.orderFrontRegardless()

    def clear(self) -> None:
        self.view.layer().setSublayers_([])
        self.window.orderOut_(None)


class GuidePanel(FloatingPage):
    """The steps, Next, Do this step for me and Stop, at the bottom of the screen."""

    PAGE = "guide.html"
    WIDTH, HEIGHT = 460, 210
    at_top = False
    rect = (0.0, 0.0, 0.0, 0.0)  # Quartz points (x, y, width, height), top-left origin.

    def __init__(self, controller, service):
        super().__init__(controller, service)
        self.window.setSharingType_(AK.NSWindowSharingNone)
        self.window.setLevel_(AK.NSPopUpMenuWindowLevel + 2)

    def place(self) -> None:
        screen = AK.NSScreen.mainScreen()
        visible, full = screen.visibleFrame(), screen.frame()
        x = visible.origin.x + (visible.size.width - self.WIDTH) / 2
        y = (
            visible.origin.y + visible.size.height - self.HEIGHT - 8
            if self.at_top
            else visible.origin.y + 16
        )
        self.window.setFrame_display_(NSMakeRect(x, y, self.WIDTH, self.HEIGHT), True)
        self.rect = (x, full.size.height - (y + self.HEIGHT), float(self.WIDTH), float(self.HEIGHT))

    def appear(self) -> None:
        if self.controller.quitting:
            return
        if not self.loaded:
            self.load()
        self.at_top = False
        self.place()
        self.window.orderFrontRegardless()
        self.send("window.BridgeGuide && window.BridgeGuide.poll()")

    def keep_clear_of(self, step: dict) -> None:
        """If the spot to click is under the panel, the panel moves to the other end."""
        spots = [list(p) for p in step.get("points") or []]
        if step.get("frame"):
            f = step["frame"]
            spots.append([f[0] + f[2] / 2, f[1] + f[3] / 2])
        left, top, width, height = self.rect
        if any(
            left - 30 <= x <= left + width + 30 and top - 60 <= y <= top + height + 30
            for x, y in spots
        ):
            self.at_top = not self.at_top
            self.place()

    def resigned(self) -> None:
        pass  # Stay visible while you work in other apps.

    def show(self) -> None:
        self.appear()


class GuideHud:
    """The guide engine's handle on the overlay and the panel, from any thread."""

    def __init__(self, panel: GuidePanel):
        self.panel = panel
        self.overlay: GuideOverlay | None = None
        self.window_was_open = False

    def _on_main(self, work, wait: float = 2.0) -> None:
        from PyObjCTools import AppHelper

        done = threading.Event()

        def run():
            try:
                work()
            finally:
                done.set()

        AppHelper.callAfter(run)
        done.wait(wait)

    def begin(self) -> None:
        """The panel appears and Bridge's own window steps aside for the app you're in."""

        def work():
            if self.overlay is None:
                self.overlay = GuideOverlay()
            self.panel.appear()
            window = getattr(self.panel.controller, "window", None)
            self.window_was_open = bool(window is not None and window.window.isVisible())
            if self.window_was_open:
                window.window.orderOut_(None)
            AK.NSApplication.sharedApplication().deactivate()

        self._on_main(work)
        time.sleep(0.5)

    def point(self, step: dict) -> None:
        def work():
            self.panel.keep_clear_of(step)
            if self.overlay is not None:
                self.overlay.show(step)

        self._on_main(work)

    def clear(self) -> None:
        self._on_main(lambda: self.overlay.clear() if self.overlay is not None else None)

    def step_aside(self) -> None:
        self.clear()

    def panel_rect(self) -> tuple[float, float, float, float]:
        return self.panel.rect

    def end(self) -> None:
        self.clear()
        if self.window_was_open:
            self.window_was_open = False
            window = getattr(self.panel.controller, "window", None)
            if window is not None:
                self._on_main(lambda: window.show("chat"))
