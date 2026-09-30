"""Native macOS voice panel. Imported only by the macOS launcher on its main thread."""

from __future__ import annotations

import AppKit as AK
from Foundation import NSMakePoint, NSMakeRect, NSObject

from app.desktop.service import ServiceState
from app.desktop.voice import IDLE_STATES, VoiceState


class PanelActions(NSObject):
    def toggle_(self, sender):
        self.owner.toggle()

    def perform_(self, sender):
        self.owner.callbacks[sender.tag()]()


class VoicePanel:
    """AppKit renders snapshots; microphone/model/API work stays on service threads."""

    WIDTH = 480
    HEIGHT = 680

    def __init__(self, controller, settings):
        self.controller = controller
        self.settings = settings
        self.callbacks = []
        self.actions = PanelActions.alloc().init()
        self.actions.owner = self
        self.status_item = None
        self.window = AK.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.WIDTH, self.HEIGHT),
            AK.NSWindowStyleMaskTitled
            | AK.NSWindowStyleMaskClosable
            | AK.NSWindowStyleMaskFullSizeContentView,
            AK.NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("Bridge · Voice")
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(AK.NSWindowTitleHidden)
        self.window.setReleasedWhenClosed_(False)
        self.window.setFloatingPanel_(True)
        self.window.setHidesOnDeactivate_(False)
        self.window.setLevel_(AK.NSFloatingWindowLevel)
        self.window.setMovableByWindowBackground_(True)
        self.view = AK.NSVisualEffectView.alloc().initWithFrame_(
            NSMakeRect(0, 0, self.WIDTH, self.HEIGHT)
        )
        self.view.setMaterial_(AK.NSVisualEffectMaterialPopover)
        self.view.setBlendingMode_(AK.NSVisualEffectBlendingModeBehindWindow)
        self.view.setState_(AK.NSVisualEffectStateActive)
        self.window.setContentView_(self.view)
        self._label("Bridge", 28, 32, 300, 40, 30, bold=True)
        self._label("YOUR MAC, WITH A WORD", 29, 77, 410, 18, 11, secondary=True)
        remote = settings.voice_stt_provider == "openai"
        provider = "Local Whisper" if not remote else "OpenAI audio upload"
        model = "OpenAI" if settings.llm_provider == "openai" else "Local Ollama"
        self._label(
            f"{provider}  ·  {model} agent  ·  Spoken replies", 28, 102, 424, 34, 12, secondary=True
        )

        self._card(28, 145, 424, 108)
        self.state_label = self._label("●  Microphone off", 44, 158, 392, 24, 17, bold=True)
        self.message_label = self._label("", 44, 191, 392, 54, 12, secondary=True)
        self.speak_background = self._card(28, 267, 310, 44, AK.NSColor.controlAccentColor())
        self.speak_button = self._button("Speak now", 28, 267, 310, 44, controller.speak_once)
        self.speak_button.setBezelColor_(AK.NSColor.controlAccentColor())
        self.speak_button.setBezelStyle_(AK.NSBezelStyleRegularSquare)
        self.speak_button.setBordered_(False)
        self.speak_button.setContentTintColor_(AK.NSColor.whiteColor())
        self.stop_button = self._button("Stop", 348, 267, 104, 44, controller.stop_voice)
        self.speak_button.setToolTip_("Record one command. No wake-word model required.")
        self.stop_button.setToolTip_(
            "Stop listening or request cancellation. Completed actions are not undone."
        )

        self._card(28, 326, 424, 98)
        self.wake_label = self._label("Wake word", 44, 339, 216, 22, 14, bold=True)
        self.wake_detail = self._label("", 44, 367, 216, 43, 11, secondary=True)
        self.wake_button = self._button("Enable", 292, 337, 144, 32, controller.start_voice)
        self.setup_button = self._button(
            "Wake-word setup", 292, 377, 144, 30, controller.open_wakeword_guide
        )

        self._label("LAST COMMAND", 28, 444, 424, 18, 11, secondary=True)
        self.transcript_label = self._label("Your words will appear here.", 28, 469, 424, 36, 14)
        self.result_label = self._label("", 28, 509, 424, 44, 12, secondary=True)
        self.dashboard_button = self._button(
            "Open dashboard", 28, 565, 242, 36, controller.open_dashboard
        )
        self.service_button = self._button("Start service", 280, 565, 172, 36, controller.start)
        self.service_label = self._label("", 28, 607, 424, 27, 11, secondary=True)
        self._button("Microphone settings", 28, 642, 168, 26, controller.open_microphone_settings)
        self._button("Voice guide", 202, 642, 120, 26, controller.open_voice_guide)
        self._button("Quit Bridge", 330, 642, 122, 26, controller.quit)
        self.refresh()

    def _rect(self, x, top, width, height):
        return NSMakeRect(x, self.HEIGHT - top - height, width, height)

    def _card(self, x, top, width, height, color=None):
        card = AK.NSBox.alloc().initWithFrame_(self._rect(x, top, width, height))
        card.setBoxType_(AK.NSBoxCustom)
        card.setTitlePosition_(AK.NSNoTitle)
        card.setBorderType_(AK.NSNoBorder)
        card.setCornerRadius_(14)
        card.setFillColor_(color or AK.NSColor.controlBackgroundColor())
        self.view.addSubview_(card)
        return card

    def _label(self, text, x, top, width, height, size, *, bold=False, secondary=False):
        label = AK.NSTextField.wrappingLabelWithString_(text)
        label.setFrame_(self._rect(x, top, width, height))
        label.setFont_(
            AK.NSFont.systemFontOfSize_weight_(
                size, AK.NSFontWeightSemibold if bold else AK.NSFontWeightRegular
            )
        )
        label.setTextColor_(
            AK.NSColor.secondaryLabelColor() if secondary else AK.NSColor.labelColor()
        )
        label.setSelectable_(True)
        self.view.addSubview_(label)
        return label

    def _button(self, title, x, top, width, height, callback):
        button = AK.NSButton.buttonWithTitle_target_action_(title, self.actions, "perform:")
        button.setFrame_(self._rect(x, top, width, height))
        button.setBezelStyle_(AK.NSBezelStyleRounded)
        button.setFont_(AK.NSFont.systemFontOfSize_(13))
        button.setTag_(len(self.callbacks))
        self.callbacks.append(callback)
        self.view.addSubview_(button)
        return button

    def attach(self, status_item):
        """Replace the dropdown with a direct panel toggle, retaining the native status item."""
        self.status_item = status_item
        status_item.setMenu_(None)
        status_item.button().setTarget_(self.actions)
        status_item.button().setAction_("toggle:")
        status_item.button().setToolTip_("Open Bridge voice controls")

    def toggle(self):
        if self.window.isVisible():
            self.window.orderOut_(None)
            return
        if self.status_item is not None:
            button = self.status_item.button()
            anchor = button.window().convertRectToScreen_(button.frame())
            screen = button.window().screen().visibleFrame()
            x = min(
                max(anchor.origin.x + anchor.size.width - self.WIDTH, screen.origin.x),
                screen.origin.x + screen.size.width - self.WIDTH,
            )
            y = max(screen.origin.y, anchor.origin.y - self.HEIGHT - 8)
            self.window.setFrameOrigin_(NSMakePoint(x, y))
        else:
            self.window.center()
        self.refresh()
        AK.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def refresh(self):
        voice = self.controller.voice
        status = voice.status
        service = self.controller.service.status
        busy = voice.active
        idle = not busy and not self.controller.quitting
        titles = {
            VoiceState.STOPPED: "Microphone off",
            VoiceState.READY: "Ready for you",
            VoiceState.STARTING: "Getting ready…",
            VoiceState.LISTENING: "Listening for a wake word",
            VoiceState.RECORDING: "Speak now",
            VoiceState.TRANSCRIBING: "Transcribing…",
            VoiceState.PROCESSING: "Working…",
            VoiceState.SPEAKING: "Speaking…",
            VoiceState.APPROVAL: "Your approval is needed",
            VoiceState.STOPPING: "Stopping…",
            VoiceState.FAILED: "Let’s get voice ready",
        }
        self.state_label.setStringValue_("●  " + titles[status.state])
        color = (
            AK.NSColor.systemOrangeColor()
            if status.state in {VoiceState.FAILED, VoiceState.APPROVAL}
            else (AK.NSColor.controlAccentColor() if busy else AK.NSColor.labelColor())
        )
        self.state_label.setTextColor_(color)
        self.message_label.setStringValue_(status.message)
        self.message_label.setToolTip_(status.message)
        self.speak_button.setEnabled_(idle)
        self.speak_button.setAlphaValue_(1.0 if idle else 0.45)
        self.speak_background.setAlphaValue_(1.0 if idle else 0.45)
        self.stop_button.setEnabled_(
            busy and status.state != VoiceState.STOPPING and not self.controller.quitting
        )
        self.wake_label.setStringValue_("Wake word · " + voice.wake_phrase)
        ready = voice.wake_model_ready
        self.wake_detail.setStringValue_(
            "Ready to enable · stays local" if ready else "Model missing. Speak now still works."
        )
        self.wake_button.setEnabled_(idle and ready)
        self.setup_button.setEnabled_(not self.controller.quitting)
        self.setup_button.setToolTip_("Train and install a model for the words ‘Hey Bridge’.")
        self.transcript_label.setStringValue_(
            self._preview(status.transcript, 115) or "Your words will appear here."
        )
        self.result_label.setStringValue_(self._preview(status.response, 190))
        self.dashboard_button.setTitle_(
            "Review action in dashboard"
            if status.state == VoiceState.APPROVAL
            else "Open dashboard"
        )
        self.dashboard_button.setEnabled_(
            service.state == ServiceState.RUNNING and not self.controller.quitting
        )
        self.service_button.setEnabled_(
            service.state in {ServiceState.STOPPED, ServiceState.FAILED}
            and not self.controller.quitting
        )
        self.service_button.setTitle_(
            "Service running" if service.state == ServiceState.RUNNING else "Start service"
        )
        self.service_label.setStringValue_(self._preview(service.message, 120))
        self.service_label.setToolTip_(service.message)
        if self.status_item is not None:
            self.status_item.button().setToolTip_("Bridge · " + titles[status.state])
            # Keep microphone use apparent even with the panel closed.
            self.status_item.button().setTitle_(
                "Bridge ●" if busy and status.state not in IDLE_STATES else "Bridge"
            )

    @staticmethod
    def _preview(text, limit):
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1] + "…"
