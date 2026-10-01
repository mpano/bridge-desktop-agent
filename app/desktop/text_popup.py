"""The "act on selected text" popup (⌃⌥Space). AppKit on the main thread only.

It is a non-activating panel, like Spotlight: the app you were typing in stays active,
so "Replace" can paste the result straight back into it.
"""

from __future__ import annotations

import threading

import AppKit as AK
import httpx
from Foundation import NSMakePoint, NSMakeRect, NSObject
from PyObjCTools import AppHelper

from app.desktop.panel_style import (
    AMBER,
    FAINT,
    GREEN,
    MUTED,
    TEXT,
    PanelCanvas,
    StyledButton,
    color,
)
from app.desktop.selection import (
    ACCESSIBILITY_SETTINGS,
    accessibility_allowed,
    copy_text,
    paste_text,
)
from app.desktop.service import ServiceState

WIDTH, HEIGHT, PAD = 460, 452, 18
BUTTONS = [
    ("improve", "Improve"),
    ("shorter", "Shorter"),
    ("grammar", "Fix grammar"),
    ("translate", "Translate"),
    ("summarize", "Summarize"),
    ("explain", "Explain"),
    ("reply", "Draft reply"),
    ("remind", "Remind me"),
]


class PopupActions(NSObject):
    def perform_(self, sender):
        self.owner.callbacks[sender.tag()]()

    def dismiss_(self, sender):
        self.owner.close()


class TextPopup:
    def __init__(self, controller, settings):
        self.controller, self.settings = controller, settings
        self.callbacks = []
        self.actions = PopupActions.alloc().init()
        self.actions.owner = self
        self.busy = False
        self.last_instruction = ""
        self.window = AK.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT),
            AK.NSWindowStyleMaskTitled
            | AK.NSWindowStyleMaskClosable
            | AK.NSWindowStyleMaskFullSizeContentView
            | AK.NSWindowStyleMaskNonactivatingPanel,
            AK.NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("Bridge")
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(AK.NSWindowTitleHidden)
        self.window.setAppearance_(AK.NSAppearance.appearanceNamed_(AK.NSAppearanceNameDarkAqua))
        self.window.setFloatingPanel_(True)
        self.window.setLevel_(AK.NSFloatingWindowLevel)
        self.window.setHidesOnDeactivate_(False)
        self.window.setBecomesKeyOnlyIfNeeded_(False)
        self.window.setReleasedWhenClosed_(False)
        self.window.setMovableByWindowBackground_(True)
        self.window.setCollectionBehavior_(
            AK.NSWindowCollectionBehaviorMoveToActiveSpace
            | AK.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self.view = PanelCanvas.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        self.window.setContentView_(self.view)
        self._build()

    # Layout -------------------------------------------------------------------------------

    def _label(self, text, x, top, width, height, size, tint=TEXT, bold=False):
        label = AK.NSTextField.labelWithString_(text)
        label.setFrame_(NSMakeRect(x, top, width, height))
        weight = AK.NSFontWeightSemibold if bold else AK.NSFontWeightRegular
        label.setFont_(AK.NSFont.systemFontOfSize_weight_(size, weight))
        label.setTextColor_(color(tint))
        label.setLineBreakMode_(AK.NSLineBreakByTruncatingTail)
        self.view.addSubview_(label)
        return label

    def _button(self, title, x, top, width, height, callback, style="secondary", icon=None):
        button = StyledButton.alloc().initWithFrame_(NSMakeRect(x, top, width, height))
        button.setTitle_(title)
        button.setTarget_(self.actions)
        button.setAction_("perform:")
        button.setBordered_(False)
        button.setButtonType_(AK.NSButtonTypeMomentaryPushIn)
        button.setAccessibilityLabel_(title)
        button.style, button.icon, button.subtitle, button.tint = style, icon, "", TEXT
        button.font_size = 12
        button.setTag_(len(self.callbacks))
        self.callbacks.append(callback)
        self.view.addSubview_(button)
        return button

    def _text_area(self, top, height, editable=True):
        scroll = AK.NSScrollView.alloc().initWithFrame_(
            NSMakeRect(PAD, top, WIDTH - 2 * PAD, height)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(AK.NSNoBorder)
        scroll.setDrawsBackground_(True)
        scroll.setBackgroundColor_(color("0B1322"))
        scroll.setWantsLayer_(True)
        scroll.layer().setCornerRadius_(10)
        text = AK.NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH - 2 * PAD, height))
        text.setEditable_(editable)
        text.setRichText_(False)
        text.setDrawsBackground_(False)
        text.setFont_(AK.NSFont.systemFontOfSize_(13))
        text.setTextColor_(color(TEXT))
        text.setInsertionPointColor_(color(TEXT))
        text.textContainer().setLineFragmentPadding_(8)
        text.setTextContainerInset_((0, 6))
        text.setAutoresizingMask_(AK.NSViewWidthSizable)
        scroll.setDocumentView_(text)
        self.view.addSubview_(scroll)
        return text

    def _build(self):
        self.source_label = self._label("Selected text", PAD, 30, 300, 16, 11, FAINT, True)
        self.input = self._text_area(50, 74)
        gap = 8
        width = (WIDTH - 2 * PAD - 3 * gap) / 4
        self.action_buttons = []
        for index, (key, title) in enumerate(BUTTONS):
            row, column = divmod(index, 4)
            button = self._button(
                title,
                PAD + column * (width + gap),
                134 + row * 36,
                width,
                28,
                lambda key=key: self.run(key),
            )
            self.action_buttons.append(button)
        field = AK.NSTextField.alloc().initWithFrame_(NSMakeRect(PAD, 212, WIDTH - 2 * PAD, 30))
        field.setPlaceholderString_("Or tell Bridge what to do with it…  (Return)")
        field.setBezelStyle_(AK.NSTextFieldRoundedBezel)
        field.setFont_(AK.NSFont.systemFontOfSize_(13))
        field.setTarget_(self.actions)
        field.setAction_("perform:")
        field.setTag_(len(self.callbacks))
        self.callbacks.append(lambda: self.run("custom"))
        self.view.addSubview_(field)
        self.instruction = field
        self.status = self._label("", PAD, 250, WIDTH - 2 * PAD, 16, 11, MUTED)
        self.output = self._text_area(270, 132)
        buttons = [
            ("Replace", self.replace, "primary"),
            ("Copy", self.copy, "secondary"),
            ("Ask Bridge", self.ask_bridge, "secondary"),
            ("Close", self.close, "secondary"),
        ]
        width = (WIDTH - 2 * PAD - 3 * gap) / 4
        self.result_buttons = {}
        for index, (title, callback, style) in enumerate(buttons):
            self.result_buttons[title] = self._button(
                title, PAD + index * (width + gap), 412, width, 30, callback, style
            )
        closer = AK.NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 0))
        closer.setKeyEquivalent_("\x1b")
        closer.setTarget_(self.actions)
        closer.setAction_("dismiss:")
        self.view.addSubview_(closer)

    # Behaviour ----------------------------------------------------------------------------

    def open(self, text: str, app_name: str, allowed: bool) -> None:
        self.input.setString_(text)
        self.output.setString_("")
        self.instruction.setStringValue_("")
        self.source_label.setStringValue_(
            f"Selected in {app_name}" if app_name else "Selected text"
        )
        if not allowed:
            self._status(
                "Turn on Bridge in Accessibility settings, then press ⌃⌥Space again — or paste text above.",
                AMBER,
            )
            self.result_buttons["Ask Bridge"].setTitle_("Allow…")
        elif not text.strip():
            self._status("Nothing was selected. Type or paste text above.", AMBER)
        else:
            self._status("Choose an action, or type what to do.", MUTED)
        self._refresh_buttons()
        mouse = AK.NSEvent.mouseLocation()
        screen = next(
            (s for s in AK.NSScreen.screens() if AK.NSMouseInRect(mouse, s.frame(), False)),
            AK.NSScreen.mainScreen(),
        ).visibleFrame()
        x = min(
            max(mouse.x - WIDTH / 2, screen.origin.x + 8),
            screen.origin.x + screen.size.width - WIDTH - 8,
        )
        y = min(
            max(mouse.y - HEIGHT - 16, screen.origin.y + 8),
            screen.origin.y + screen.size.height - HEIGHT - 8,
        )
        self.window.setFrameOrigin_(NSMakePoint(x, y))
        self.window.makeKeyAndOrderFront_(None)
        self.window.makeFirstResponder_(self.instruction if text.strip() else self.input)

    def close(self):
        self.window.orderOut_(None)
        self.result_buttons["Ask Bridge"].setTitle_("Ask Bridge")

    def _status(self, message, tint=MUTED):
        self.status.setStringValue_(message)
        self.status.setTextColor_(color(tint))

    def _refresh_buttons(self):
        has_result = bool(self.output.string().strip())
        for button in self.action_buttons:
            button.setEnabled_(not self.busy)
        self.result_buttons["Replace"].setEnabled_(has_result and not self.busy)
        self.result_buttons["Copy"].setEnabled_(has_result and not self.busy)
        for button in self.result_buttons.values():
            button.setNeedsDisplay_(True)

    def run(self, action: str) -> None:
        text = self.input.string().strip()
        if self.busy:
            return
        if not text:
            self._status("There's no text yet. Type or paste some above.", AMBER)
            return
        if action == "remind":
            self._send_to_agent(f"Create a reminder from this text:\n\n{text}")
            return
        instruction = self.instruction.stringValue().strip()
        if action == "custom" and not instruction:
            self._status("Type what you'd like done, then press Return.", AMBER)
            return
        service = self.controller.service.status
        if service.state != ServiceState.RUNNING or not service.url:
            self._status("Bridge's local service isn't running yet.", AMBER)
            return
        self.busy = True
        self.last_instruction = instruction if action == "custom" else action
        self._status("Working on it…", MUTED)
        self._refresh_buttons()
        payload = {"action": action, "text": text, "instruction": instruction}
        token = self.settings.api_token.get_secret_value()

        def work():
            try:
                with httpx.Client(timeout=60, trust_env=False) as client:
                    response = client.post(
                        service.url + "/api/v1/text/transform",
                        json=payload,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                data = response.json() if response.content else {}
                if response.status_code == 200:
                    AppHelper.callAfter(self._done, data["result"], None)
                else:
                    AppHelper.callAfter(self._done, None, data.get("detail", "That didn't work."))
            except Exception:
                AppHelper.callAfter(self._done, None, "Couldn't reach Bridge. Try again.")

        threading.Thread(target=work, name="bridge-text-action", daemon=True).start()

    def _done(self, result, error):
        self.busy = False
        if error:
            self._status(str(error), AMBER)
        else:
            self.output.setString_(result)
            self._status("Done. Replace, copy, or edit it first.", GREEN)
        self._refresh_buttons()

    def replace(self):
        result = self.output.string()
        if not result.strip():
            return
        self.close()
        # Give the original app its keyboard focus back before pasting into it.
        AppHelper.callLater(0.15, paste_text, result)

    def copy(self):
        copy_text(self.output.string())
        self._status("Copied to the clipboard.", GREEN)

    def ask_bridge(self):
        if self.result_buttons["Ask Bridge"].title() == "Allow…":
            accessibility_allowed(prompt=True)
            AK.NSWorkspace.sharedWorkspace().openURL_(
                AK.NSURL.URLWithString_(ACCESSIBILITY_SETTINGS)
            )
            return
        text = self.input.string().strip()
        instruction = self.instruction.stringValue().strip() or "Help me with this text"
        self._send_to_agent(f"{instruction}:\n\n{text}")

    def _send_to_agent(self, request: str) -> None:
        if self.controller.submit_text(request):
            self.close()
            self.controller.show_panel()
        else:
            self._status("Bridge is busy. Try again in a moment.", AMBER)
