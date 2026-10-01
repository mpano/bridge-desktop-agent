"""Bridge's companion window. AppKit stays on the launcher's main thread.

It behaves like a normal window (goes behind other apps, can be minimized) unless the
user pins it on top. Its size and position are remembered.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

import AppKit as AK
import objc
from Foundation import NSMakePoint, NSMakeRect, NSObject

from app.desktop.panel_style import (
    AMBER,
    CYAN,
    FAINT,
    GREEN,
    MUTED,
    TEXT,
    BrandMark,
    PanelCanvas,
    StyledButton,
    Surface,
    VoiceOrb,
    color,
    symbol,
)
from app.desktop.service import ServiceState
from app.desktop.voice import IDLE_STATES, VoiceState
from app.voice.paths import BRIDGE_SUPPORT_DIR

PREFERENCES = BRIDGE_SUPPORT_DIR / "panel.json"
QUICK_ACTIONS = [
    ("Plan my day", "Plan my day"),
    ("Inbox", "What needs my attention?"),
    ("Brief me", "Brief me"),
    ("Now playing", "What's playing on Spotify?"),
]


class PanelActions(NSObject):
    def toggle_(self, sender):
        event = AK.NSApplication.sharedApplication().currentEvent()
        secondary = event is not None and (
            event.type() == AK.NSEventTypeRightMouseUp
            or event.modifierFlags() & AK.NSEventModifierFlagControl
        )
        if secondary:
            self.owner.show_menu()
        else:
            self.owner.toggle()

    def perform_(self, sender):
        self.owner.callbacks[sender.tag()]()

    def dismiss_(self, sender):
        self.owner.hide()

    def windowWillClose_(self, notification):
        # The red close button: Bridge keeps running in the menu bar, without a Dock icon.
        self.owner.set_dock_visible(False)

    def about_(self, sender):
        from app.desktop.bundle import VERSION
        from app.desktop.icon import app_icon_image

        app = AK.NSApplication.sharedApplication()
        app.activateIgnoringOtherApps_(True)
        app.orderFrontStandardAboutPanelWithOptions_(
            {
                "ApplicationName": "Bridge",
                "ApplicationIcon": app_icon_image(256),
                "ApplicationVersion": VERSION,
                "Version": "",
                "Copyright": "A local macOS desktop agent.",
            }
        )

    @objc.typedSelector(b"v@:@@")
    def handleReopen_withReplyEvent_(self, event, reply):
        # Opening Bridge.app again (Finder, Spotlight, Dock) while it runs shows the panel.
        self.owner.show()

    def listenForReopen(self):
        AK.NSAppleEventManager.sharedAppleEventManager().setEventHandler_andSelector_forEventClass_andEventID_(  # noqa: E501
            self, "handleReopen:withReplyEvent:", fourcc(b"aevt"), fourcc(b"rapp")
        )


def fourcc(code: bytes) -> int:
    return int.from_bytes(code, "big")


def load_preferences() -> dict:
    try:
        return json.loads(PREFERENCES.read_text())
    except (OSError, ValueError):
        return {}


def save_preferences(values: dict) -> None:
    try:
        PREFERENCES.parent.mkdir(parents=True, exist_ok=True)
        PREFERENCES.write_text(json.dumps(values))
    except OSError:
        pass


class VoicePanel:
    WIDTH = 440
    HEIGHT = 748
    PAD = 20

    def __init__(self, controller, settings):
        self.controller = controller
        self.settings = settings
        self.callbacks = []
        self.actions = PanelActions.alloc().init()
        self.actions.owner = self
        self.status_item = None
        self.menu = None
        self.review_sheet = None
        self.history_key = None
        self.preferences = load_preferences()
        screen = AK.NSScreen.mainScreen()
        self.viewport_height = (
            min(self.HEIGHT, screen.visibleFrame().size.height - 52) if screen else self.HEIGHT
        )
        self.window = AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.WIDTH, self.viewport_height),
            AK.NSWindowStyleMaskTitled
            | AK.NSWindowStyleMaskClosable
            | AK.NSWindowStyleMaskMiniaturizable
            | AK.NSWindowStyleMaskFullSizeContentView,
            AK.NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("Bridge")
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(AK.NSWindowTitleHidden)
        self.window.setAppearance_(AK.NSAppearance.appearanceNamed_(AK.NSAppearanceNameDarkAqua))
        self.window.setBackgroundColor_(color("0C1423"))
        self.window.setReleasedWhenClosed_(False)
        self.window.setHidesOnDeactivate_(False)
        self.window.setMovableByWindowBackground_(True)
        # Show on the current Space instead of dragging the user to another desktop.
        self.window.setCollectionBehavior_(AK.NSWindowCollectionBehaviorMoveToActiveSpace)
        self.window.setDelegate_(self.actions)
        self.dock_icon = None
        self.positioned = self.window.setFrameUsingName_("BridgePanel")
        self.window.setFrameAutosaveName_("BridgePanel")
        self._apply_pin()

        self.view = PanelCanvas.alloc().initWithFrame_(NSMakeRect(0, 0, self.WIDTH, self.HEIGHT))
        self.scroll = AK.NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, 0, self.WIDTH, self.viewport_height)
        )
        self.scroll.setDrawsBackground_(False)
        self.scroll.setBorderType_(AK.NSNoBorder)
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setScrollerStyle_(AK.NSScrollerStyleOverlay)
        self.scroll.setAutohidesScrollers_(True)
        self.scroll.setDocumentView_(self.view)
        self.window.setContentView_(self.scroll)
        self._build_header()
        self._build_voice_card()
        self._build_ask()
        self._build_history()
        self._build_wake_row()
        self._build_footer()
        # Esc hides the window; a zero-size button carries the key equivalent.
        closer = AK.NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 0))
        closer.setKeyEquivalent_("\x1b")
        closer.setTarget_(self.actions)
        closer.setAction_("dismiss:")
        closer.setAccessibilityElement_(False)
        self.view.addSubview_(closer)
        self.refresh()

    # Layout -------------------------------------------------------------------------------

    def _build_header(self):
        logo = BrandMark.alloc().initWithFrame_(NSMakeRect(self.PAD, 40, 40, 40))
        logo.setAccessibilityElement_(False)
        # BrandMark draws in a 64-point space; scale it into the smaller header mark.
        logo.setBoundsSize_((64, 64))
        self.view.addSubview_(logo)
        self._label("Bridge", 70, 38, 180, 26, 21, bold=True)
        self.header_status = self._label("", 70, 64, 220, 16, 11, tint=GREEN)
        right = self.WIDTH - self.PAD
        self.pin_button = self._button(
            "Keep on top", right - 124, 44, 28, 28, self.toggle_pin, icon="pin", style="icon"
        )
        self._button(
            "Open dashboard",
            right - 92,
            44,
            28,
            28,
            self.controller.open_dashboard,
            icon="macwindow",
            style="icon",
        )
        self._button(
            "Help",
            right - 60,
            44,
            28,
            28,
            self.controller.open_voice_guide,
            icon="questionmark.circle",
            style="icon",
        )
        self._button(
            "Quit Bridge", right - 28, 44, 28, 28, self.controller.quit, icon="power", style="icon"
        )

    def _build_voice_card(self):
        top, width = 96, self.WIDTH - 2 * self.PAD
        self._card(self.PAD, top, width, 168, "hero")
        self.orb = VoiceOrb.alloc().initWithFrame_(NSMakeRect(self.PAD + 8, top + 14, 124, 124))
        self.orb.setBoundsSize_((172, 172))  # Drawn for 172 pt; scale into 124.
        self.orb.setAccessibilityElement_(False)
        self.view.addSubview_(self.orb)
        self.mic_caption = self._label(
            "MICROPHONE OFF", self.PAD + 8, top + 140, 124, 14, 9, tint=MUTED, center=True
        )
        left = self.PAD + 144
        inner = width - 144 - 16
        self.state_label = self._label(
            "Ready when you are", left, top + 16, inner, 26, 19, bold=True
        )
        self.message_label = self._label("", left, top + 44, inner, 52, 12, tint=MUTED)
        self.speak_button = self._button(
            "Speak now",
            left,
            top + 104,
            inner - 54,
            38,
            self.controller.speak_once,
            icon="waveform",
            style="primary",
        )
        self.stop_button = self._button(
            "Stop",
            left + inner - 46,
            top + 104,
            46,
            38,
            self.controller.stop_voice,
            icon="stop.fill",
        )
        self.stop_button.title_hidden = True
        self.speak_button.font_size = 13
        self.shortcut_label = self._label("", left, top + 146, inner, 14, 9, tint=FAINT)
        self.speak_button.setToolTip_("Record one command.")
        self.stop_button.setToolTip_(
            "Stop listening or request cancellation. Completed actions are not undone."
        )

    def _build_ask(self):
        top, width = 280, self.WIDTH - 2 * self.PAD
        field = AK.NSTextField.alloc().initWithFrame_(NSMakeRect(self.PAD, top, width - 50, 36))
        field.setPlaceholderString_("Ask Bridge anything…")
        field.setFont_(AK.NSFont.systemFontOfSize_(14))
        field.setBezelStyle_(AK.NSTextFieldRoundedBezel)
        field.setFocusRingType_(AK.NSFocusRingTypeExterior)
        field.setTarget_(self.actions)
        field.setAction_("perform:")
        field.setTag_(len(self.callbacks))
        self.callbacks.append(self.submit_typed)
        field.setAccessibilityLabel_("Type a request for Bridge")
        self.view.addSubview_(field)
        self.ask_field = field
        self.ask_button = self._button(
            "Send",
            self.WIDTH - self.PAD - 42,
            top,
            42,
            36,
            self.submit_typed,
            icon="paperplane.fill",
            style="primary",
        )
        self.ask_button.title_hidden = True
        self.ask_button.setToolTip_("Send (Return)")
        # One-click requests.
        self.quick_buttons = []
        gap = 8
        chip = (width - gap * (len(QUICK_ACTIONS) - 1)) / len(QUICK_ACTIONS)
        for index, (label, request) in enumerate(QUICK_ACTIONS):
            button = self._button(
                label,
                self.PAD + index * (chip + gap),
                top + 46,
                chip,
                28,
                lambda request=request: self.run_quick(request),
            )
            button.font_size = 11
            button.setToolTip_(f"Ask: “{request}”")
            self.quick_buttons.append(button)

    def _build_history(self):
        top, width = 368, self.WIDTH - 2 * self.PAD
        self._card(self.PAD, top, width, 262)
        self._label("RECENT", self.PAD + 16, top + 14, 120, 14, 10, tint=FAINT, bold=True)
        self.outcome_label = self._label(
            "", self.PAD + 110, top + 13, width - 190, 16, 11, tint=FAINT
        )
        self.clear_button = self._button(
            "Clear", self.PAD + width - 62, top + 8, 50, 24, self.clear_history
        )
        self.clear_button.font_size = 11
        self.history_scroll = AK.NSScrollView.alloc().initWithFrame_(
            NSMakeRect(self.PAD + 10, top + 38, width - 20, 176)
        )
        self.history_scroll.setDrawsBackground_(False)
        self.history_scroll.setBorderType_(AK.NSNoBorder)
        self.history_scroll.setHasVerticalScroller_(True)
        self.history_scroll.setAutohidesScrollers_(True)
        self.history_view = AK.NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, width - 20, 176))
        self.history_view.setEditable_(False)
        self.history_view.setSelectable_(True)
        self.history_view.setDrawsBackground_(False)
        self.history_view.textContainer().setLineFragmentPadding_(6)
        self.history_view.setAutoresizingMask_(AK.NSViewWidthSizable)
        self.history_scroll.setDocumentView_(self.history_view)
        self.view.addSubview_(self.history_scroll)
        self.review_button = self._button(
            "Review action",
            self.PAD + 12,
            top + 222,
            (width - 32) / 2,
            30,
            self.review_action,
            icon="checkmark.shield",
            style="primary",
        )
        self.review_button.font_size = 12
        self.review_button.setHidden_(True)
        self.dashboard_button = self._button(
            "Open dashboard",
            self.PAD + 12,
            top + 222,
            width - 24,
            30,
            self.controller.open_dashboard,
            icon="arrow.up.right.square",
        )
        self.dashboard_button.font_size = 12

    def _build_wake_row(self):
        top, width = 642, self.WIDTH - 2 * self.PAD
        self._card(self.PAD, top, width, 64)
        self._icon("waveform", self.PAD + 14, top + 17, 28, "AF8BFF")
        self.wake_label = self._label("Hey Bridge", self.PAD + 52, top + 12, 170, 20, 14, bold=True)
        self.wake_detail = self._label("", self.PAD + 52, top + 33, width - 240, 28, 10, tint=MUTED)
        self.train_button = self._button(
            "Train my voice",
            self.PAD + width - 62 - 122,
            top + 18,
            122,
            28,
            self.train_voice,
            icon="person.wave.2",
        )
        self.train_button.font_size = 11
        self.wake_button = AK.NSSwitch.alloc().initWithFrame_(
            NSMakeRect(self.PAD + width - 54, top + 19, 42, 26)
        )
        self.wake_button.setTarget_(self.actions)
        self.wake_button.setAction_("perform:")
        self.wake_button.setTag_(len(self.callbacks))
        self.callbacks.append(self.toggle_wake)
        self.wake_button.setAccessibilityLabel_("Listen for Hey Bridge")
        self.view.addSubview_(self.wake_button)

    def _build_footer(self):
        top = 718
        self.service_label = self._label("", self.PAD + 4, top + 4, 250, 16, 11, tint=GREEN)
        self.service_button = self._button(
            "Start service",
            self.WIDTH - self.PAD - 110,
            top,
            110,
            24,
            self.controller.start,
            icon="arrow.clockwise",
        )
        self.service_button.font_size = 11

    # Widgets ------------------------------------------------------------------------------

    def _card(self, x, top, width, height, kind="card"):
        card = Surface.alloc().initWithFrame_(NSMakeRect(x, top, width, height))
        card.kind = kind
        card.setAccessibilityElement_(False)
        self.view.addSubview_(card)
        return card

    def _icon(self, name, x, top, size, tint):
        view = AK.NSImageView.alloc().initWithFrame_(NSMakeRect(x, top, size, size))
        view.setImage_(symbol(name, tint))
        view.setImageScaling_(AK.NSImageScaleProportionallyUpOrDown)
        view.setAccessibilityElement_(False)
        self.view.addSubview_(view)

    def _label(self, text, x, top, width, height, size, *, bold=False, tint=TEXT, center=False):
        label = AK.NSTextField.wrappingLabelWithString_(text)
        label.setFrame_(NSMakeRect(x, top, width, height))
        label.setFont_(
            AK.NSFont.systemFontOfSize_weight_(
                size, AK.NSFontWeightSemibold if bold else AK.NSFontWeightRegular
            )
        )
        label.setTextColor_(color(tint))
        label.setSelectable_(False)
        if center:
            label.setAlignment_(AK.NSTextAlignmentCenter)
        self.view.addSubview_(label)
        return label

    def _button(
        self,
        title,
        x,
        top,
        width,
        height,
        callback,
        *,
        icon=None,
        style="secondary",
        subtitle="",
        tint=CYAN,
    ):
        button = StyledButton.alloc().initWithFrame_(NSMakeRect(x, top, width, height))
        button.setTitle_(title)
        button.setTarget_(self.actions)
        button.setAction_("perform:")
        button.setBordered_(False)
        button.setButtonType_(AK.NSButtonTypeMomentaryPushIn)
        button.setAccessibilityLabel_(title)
        button.setToolTip_(title)
        button.style, button.icon, button.subtitle, button.tint = style, icon, subtitle, tint
        button.setTag_(len(self.callbacks))
        self.callbacks.append(callback)
        self.view.addSubview_(button)
        return button

    # Actions ------------------------------------------------------------------------------

    def submit_typed(self):
        text = self.ask_field.stringValue().strip()
        if text and self.controller.submit_text(text):
            self.ask_field.setStringValue_("")
        self.refresh()

    def run_quick(self, request):
        self.controller.submit_text(request)
        self.refresh()

    def clear_history(self):
        self.controller.voice.clear_history()
        self.refresh()

    def train_voice(self):
        self.controller.train_voice()
        self.refresh()

    def toggle_wake(self):
        if self.controller.voice.wake_enabled:
            self.controller.stop_voice()
        else:
            self.controller.start_voice()
        self.refresh()

    def _apply_pin(self):
        pinned = bool(self.preferences.get("pinned"))
        self.window.setLevel_(AK.NSFloatingWindowLevel if pinned else AK.NSNormalWindowLevel)

    def toggle_pin(self):
        self.preferences["pinned"] = not self.preferences.get("pinned")
        save_preferences(self.preferences)
        self._apply_pin()
        self.refresh()

    def review_action(self):
        from app.desktop.approval import ApprovalSheet

        review = self.controller.voice.pending_review
        if review is None or self.controller.voice.active or self.controller.quitting:
            return
        if self.window.attachedSheet() is not None:
            return

        def decide(review_id, approved):
            if not self.controller.quitting:
                self.controller.voice.confirm_review(review_id, approved)
            self.review_sheet = None
            self.refresh()

        self.review_sheet = ApprovalSheet(review, decide)
        self.review_sheet.show(self.window)

    # Window behaviour ---------------------------------------------------------------------

    def attach(self, status_item):
        self.status_item = status_item
        self.menu = status_item.menu()
        status_item.setMenu_(None)
        button = status_item.button()
        button.setTarget_(self.actions)
        button.setAction_("toggle:")
        button.sendActionOn_(AK.NSEventMaskLeftMouseUp | AK.NSEventMaskRightMouseUp)
        button.setToolTip_("Bridge — click to open, right-click for menu")

    def show_menu(self):
        """Pop up the full Bridge menu (service, voice, login, quit) from the status item."""
        if self.status_item is None or self.menu is None:
            return
        self.controller.refresh(None)
        self.status_item.setMenu_(self.menu)
        self.status_item.button().performClick_(None)
        self.status_item.setMenu_(None)

    def _place_under_status_item(self):
        if self.status_item is None:
            self.window.center()
            return
        button = self.status_item.button()
        anchor = button.window().convertRectToScreen_(button.frame())
        screen = button.window().screen().visibleFrame()
        x = min(
            max(anchor.origin.x + anchor.size.width - self.WIDTH, screen.origin.x),
            screen.origin.x + screen.size.width - self.WIDTH,
        )
        y = max(screen.origin.y, anchor.origin.y - self.window.frame().size.height - 8)
        self.window.setFrameOrigin_(NSMakePoint(x, y))

    def set_dock_visible(self, visible: bool) -> None:
        """A Dock icon while the window is open (or minimized); menu bar only otherwise."""
        app = AK.NSApplication.sharedApplication()
        policy = (
            AK.NSApplicationActivationPolicyRegular
            if visible
            else AK.NSApplicationActivationPolicyAccessory
        )
        if app.activationPolicy() != policy:
            app.setActivationPolicy_(policy)
        if visible:
            if self.dock_icon is None:
                from app.desktop.icon import app_icon_image

                self.dock_icon = app_icon_image(512)
            app.setApplicationIconImage_(self.dock_icon)

    def hide(self):
        self.window.orderOut_(None)
        self.set_dock_visible(False)

    def show(self):
        if self.controller.quitting:
            return
        self.set_dock_visible(True)
        if self.window.isMiniaturized():
            self.window.deminiaturize_(None)
        if not self.window.isVisible() and not self.positioned:
            self._place_under_status_item()
            self.positioned = True
        self.refresh()
        AK.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        if self.ask_field.isEnabled():
            self.window.makeFirstResponder_(self.ask_field)

    def toggle(self):
        """Menu-bar click: hide if Bridge is in front, otherwise bring it forward."""
        in_front = (
            self.window.isVisible()
            and not self.window.isMiniaturized()
            and self.window.isKeyWindow()
            and AK.NSApplication.sharedApplication().isActive()
        )
        if in_front:
            self.hide()
        else:
            self.show()

    # Refresh ------------------------------------------------------------------------------

    TITLES = {
        VoiceState.STOPPED: "Ready when you are",
        VoiceState.READY: "Ready for more",
        VoiceState.STARTING: "Getting ready…",
        VoiceState.LISTENING: "Listening for “Hey Bridge”",
        VoiceState.RECORDING: "I’m listening",
        VoiceState.TRANSCRIBING: "Finding your words…",
        VoiceState.PROCESSING: "On it…",
        VoiceState.SPEAKING: "Here’s what happened",
        VoiceState.APPROVAL: "Needs your OK",
        VoiceState.STOPPING: "Wrapping up…",
        VoiceState.FAILED: "Something needs attention",
    }

    def _history_text(self, history):
        text = AK.NSMutableAttributedString.alloc().init()

        def add(value, size, tint, weight=AK.NSFontWeightRegular):
            attributes = {
                AK.NSFontAttributeName: AK.NSFont.systemFontOfSize_weight_(size, weight),
                AK.NSForegroundColorAttributeName: color(tint),
            }
            text.appendAttributedString_(
                AK.NSAttributedString.alloc().initWithString_attributes_(value, attributes)
            )

        if not history:
            add("Your requests and Bridge’s answers appear here.\n\n", 12, MUTED)
            add("Try a quick action above, type a request, or click Speak now.", 12, FAINT)
            return text
        for index, entry in enumerate(reversed(history)):
            stamp = datetime.fromtimestamp(entry["at"]).strftime("%H:%M")
            mark = {"completed": "✓", "failed": "!", "confirmation_required": "◷"}.get(
                entry["status"], "·"
            )
            tint = {"failed": AMBER, "confirmation_required": AMBER}.get(entry["status"], GREEN)
            if index:
                add("\n", 8, FAINT)
            add(f"{mark} ", 12, tint, AK.NSFontWeightBold)
            add(entry["request"] or "Request", 13, TEXT, AK.NSFontWeightSemibold)
            add(f"   {stamp}\n", 10, FAINT)
            reply = "\n".join(line for line in entry["reply"].strip().splitlines() if line.strip())
            add(reply + "\n", 12, MUTED)
        return text

    def refresh(self):
        voice = self.controller.voice
        status = voice.status
        service = self.controller.service.status
        busy = voice.active
        idle = not busy and not self.controller.quitting
        pending = voice.pending_review
        connected = service.state == ServiceState.RUNNING

        self.state_label.setStringValue_(self.TITLES[status.state])
        message = status.message
        if status.state == VoiceState.STOPPED and not status.transcript:
            message = "Type below, pick a quick action, or click Speak now."
        self.message_label.setStringValue_(message)
        self.message_label.setToolTip_(message)
        self.shortcut_label.setStringValue_(getattr(self.controller, "shortcut_status", ""))
        caption = {
            VoiceState.LISTENING: "WAKE WORD ON",
            VoiceState.RECORDING: "RECORDING",
            VoiceState.STARTING: "PREPARING",
            VoiceState.STOPPING: "FINISHING",
            VoiceState.SPEAKING: "SPEAKING",
            VoiceState.PROCESSING: "WORKING",
            VoiceState.TRANSCRIBING: "TRANSCRIBING",
            VoiceState.APPROVAL: "WAITING FOR YOU",
        }.get(status.state, "MICROPHONE OFF")
        self.mic_caption.setStringValue_(caption)
        reduced = AK.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion()
        tick = time.monotonic() if self.window.isVisible() and not reduced else 0.0
        self.orb.update(status.state.value, tick)

        can_start = idle and pending is None
        self.speak_button.setEnabled_(can_start)
        self.stop_button.setEnabled_(
            busy and status.state != VoiceState.STOPPING and not self.controller.quitting
        )
        self.ask_field.setEnabled_(can_start)
        self.ask_button.setEnabled_(can_start)
        for button in self.quick_buttons:
            button.setEnabled_(can_start and connected)

        # Recent requests, only re-rendered when they change so scrolling is kept.
        history = voice.history
        key = (len(history), history[-1]["at"] if history else 0)
        if key != self.history_key:
            self.history_key = key
            self.history_view.textStorage().setAttributedString_(self._history_text(history))
            self.history_view.scrollRangeToVisible_((0, 0))
        self.clear_button.setHidden_(not history)
        outcome, tint = {
            "completed": ("✓ Done", GREEN),
            "failed": ("! Needs attention", AMBER),
            "confirmation_required": ("◷ Needs approval", AMBER),
            "cancelled": ("Stopped", MUTED),
        }.get(status.result_status, ("Working…" if busy and status.transcript else "", FAINT))
        self.outcome_label.setStringValue_(outcome)
        self.outcome_label.setTextColor_(color(tint))
        self.review_button.setHidden_(pending is None)
        self.review_button.setEnabled_(pending is not None and idle and connected)
        width = self.WIDTH - 2 * self.PAD
        if pending is not None:
            self.dashboard_button.setFrame_(
                NSMakeRect(self.PAD + 20 + (width - 32) / 2, 368 + 222, (width - 32) / 2, 30)
            )
        else:
            self.dashboard_button.setFrame_(NSMakeRect(self.PAD + 12, 368 + 222, width - 24, 30))
        self.dashboard_button.setEnabled_(connected and not self.controller.quitting)

        ready, listening = voice.wake_model_ready, voice.wake_enabled
        self.wake_button.setState_(
            AK.NSControlStateValueOn if listening else AK.NSControlStateValueOff
        )
        self.wake_button.setEnabled_(
            (listening or (can_start and ready))
            and status.state != VoiceState.STOPPING
            and not self.controller.quitting
        )
        self.wake_detail.setStringValue_(
            "Listening — say “Hey Bridge”, wait for the sound."
            if listening
            else "Ready. Switch on to listen hands-free."
            if ready
            else "Teach it your voice (1 minute)."
        )
        self.wake_detail.setTextColor_(color(GREEN if listening else MUTED))
        self.train_button.setTitle_("Retrain" if ready else "Train my voice")
        self.train_button.setEnabled_(can_start and not listening)
        self.train_button.setNeedsDisplay_(True)

        pinned = bool(self.preferences.get("pinned"))
        self.pin_button.icon = "pin.fill" if pinned else "pin"
        self.pin_button.icon_tint = CYAN if pinned else MUTED
        self.pin_button.setToolTip_(
            "Unpin (let other windows cover Bridge)"
            if pinned
            else "Keep Bridge on top of other windows"
        )
        self.pin_button.setNeedsDisplay_(True)

        agent = "OpenAI"
        self.header_status.setStringValue_(
            f"● Ready · {agent}" if connected else "● " + self._preview(service.message, 40)
        )
        self.header_status.setTextColor_(color(GREEN if connected else AMBER))
        self.service_label.setStringValue_(
            "Local service connected" if connected else self._preview(service.message, 44)
        )
        self.service_label.setTextColor_(color(FAINT if connected else AMBER))
        self.service_button.setHidden_(connected)
        self.service_button.setEnabled_(
            service.state in {ServiceState.STOPPED, ServiceState.FAILED}
            and not self.controller.quitting
        )
        if self.status_item is not None:
            self.status_item.button().setToolTip_("Bridge · " + self.TITLES[status.state])
            self.status_item.button().setTitle_(
                "Bridge ●" if busy and status.state not in IDLE_STATES else "Bridge"
            )

    @staticmethod
    def _preview(text, limit):
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1] + "…"


def install_main_menu(actions) -> None:
    """The menu bar shown while Bridge has a Dock icon. Edit gives text fields ⌘C/⌘V."""

    def item(title, action, key="", target=None, modifiers=None):
        entry = AK.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if target is not None:
            entry.setTarget_(target)
        if modifiers is not None:
            entry.setKeyEquivalentModifierMask_(modifiers)
        return entry

    def submenu(title, entries):
        menu = AK.NSMenu.alloc().initWithTitle_(title)
        for entry in entries:
            menu.addItem_(entry or AK.NSMenuItem.separatorItem())
        holder = AK.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        holder.setSubmenu_(menu)
        return holder

    shift = AK.NSEventModifierFlagCommand | AK.NSEventModifierFlagShift
    main = AK.NSMenu.alloc().initWithTitle_("MainMenu")
    main.addItem_(
        submenu(
            "Bridge",
            [
                item("About Bridge", "about:", target=actions),
                None,
                item("Hide Bridge", "hide:", "h"),
                None,
                item("Quit Bridge", "terminate:", "q"),
            ],
        )
    )
    main.addItem_(
        submenu(
            "Edit",
            [
                item("Undo", "undo:", "z"),
                item("Redo", "redo:", "z", modifiers=shift),
                None,
                item("Cut", "cut:", "x"),
                item("Copy", "copy:", "c"),
                item("Paste", "paste:", "v"),
                item("Select All", "selectAll:", "a"),
            ],
        )
    )
    window_menu = submenu(
        "Window",
        [item("Minimize", "performMiniaturize:", "m"), item("Close", "performClose:", "w")],
    )
    main.addItem_(window_menu)
    app = AK.NSApplication.sharedApplication()
    app.setMainMenu_(main)
    app.setWindowsMenu_(window_menu.submenu())
