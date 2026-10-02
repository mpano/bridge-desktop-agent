"""Dictation everywhere (⌃⌥D): speak, and Bridge types clean text where your cursor is.

Press once to start and again to finish, or just stop talking. Audio stays on this Mac
with local Whisper; only the transcript is sent for cleanup (DICTATION_CLEANUP=false
types exactly what was heard). Nothing is ever sent or submitted: the text is pasted into
the app you were in, and if you switched apps meanwhile it is copied instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from enum import StrEnum

import AppKit as AK
import httpx
from Foundation import NSMakePoint, NSMakeRect, NSObject
from PyObjCTools import AppHelper

from app.desktop.panel_style import AMBER, GREEN, TEXT, color, rounded
from app.desktop.selection import accessibility_allowed, copy_text, paste_text
from app.desktop.service import ServiceState
from app.voice.cue import play_native_sound
from app.voice.endpointing import EndpointingRecorder, NoSpeechDetected
from app.voice.errors import VoiceError
from app.voice.stt import build_stt

log = logging.getLogger(__name__)


class DictationState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    POLISHING = "polishing"


def front_app() -> dict:
    app = AK.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return {"pid": 0, "name": "", "bundle_id": ""}
    return {
        "pid": int(app.processIdentifier()),
        "name": str(app.localizedName() or ""),
        "bundle_id": str(app.bundleIdentifier() or ""),
    }


class Dictation:
    def __init__(
        self,
        settings,
        service,
        hud=None,
        *,
        stt=None,
        recorder_factory=None,
        front=front_app,
        paste=paste_text,
        copy=copy_text,
        can_type=accessibility_allowed,
        call=AppHelper.callAfter,
        cue=play_native_sound,
        clean=None,
    ):
        self.settings, self.service, self.hud = settings, service, hud
        self._stt = stt
        self.recorder_factory = recorder_factory or self._recorder
        self.front, self.paste, self.copy, self.call, self.cue = front, paste, copy, call, cue
        self.can_type = can_type
        self.clean = clean or self._clean
        self.state = DictationState.IDLE
        self.finish, self.cancel = threading.Event(), threading.Event()
        self.target: dict = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    # Called on the main thread -----------------------------------------------------------

    def toggle(self) -> None:
        if self.state == DictationState.LISTENING:
            self.finish.set()
            self._hud("working", "Finishing…")
            return
        if self.state != DictationState.IDLE:
            return
        self.target = self.front()
        self.finish.clear()
        self.cancel.clear()
        self.state = DictationState.LISTENING
        where = f" into {self.target['name']}" if self.target.get("name") else ""
        self._hud("listening", f"Listening{where}… ⌃⌥D to finish")
        if getattr(self.settings, "voice_start_sound", False) and self.cue is not None:
            with contextlib.suppress(Exception):
                self.cue()
        asyncio.run_coroutine_threadsafe(self._run(), self._event_loop())

    def stop(self) -> None:
        """Cancel: discard what was said."""
        self.cancel.set()
        if self.state == DictationState.LISTENING:
            self._hud("hidden", "")

    # Background --------------------------------------------------------------------------

    def _event_loop(self) -> asyncio.AbstractEventLoop:
        # One long-lived loop keeps the speech model loaded between dictations.
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            threading.Thread(
                target=self._loop.run_forever, name="bridge-dictation", daemon=True
            ).start()
        return self._loop

    @property
    def stt(self):
        if self._stt is None:
            self._stt = build_stt(self.settings)
        return self._stt

    def _recorder(self, cancel, finish):
        return EndpointingRecorder(
            cancel,
            silence_seconds=self.settings.dictation_pause_seconds,
            wait_seconds=8,
            threshold=self.settings.voice_activity_threshold,
            finish=finish,
            max_seconds=self.settings.dictation_max_seconds,
        )

    async def _run(self) -> None:
        try:
            text = await self._listen()
            if text and self.settings.dictation_cleanup and not self.cancel.is_set():
                self._set(DictationState.POLISHING, "working", "Tidying up…")
                text = await self.clean(text, self.target.get("name", ""))
            if self.cancel.is_set():
                self._finish("hidden", "")
            elif text:
                self.call(self._deliver, text)
        except NoSpeechDetected:
            if self.cancel.is_set():
                self._finish("hidden", "")
            else:
                self._finish("error", "I didn't hear anything.")
        except VoiceError as exc:
            self._finish("error", str(exc))
        except Exception:
            self._finish("error", "Dictation failed. Check Microphone permission for Bridge.")

    async def _listen(self) -> str:
        recorder = self.recorder_factory(self.cancel, self.finish)
        path = await recorder.record_wav(self.settings.dictation_max_seconds)
        try:
            self._set(DictationState.TRANSCRIBING, "working", "Transcribing…")
            text = (await self.stt.transcribe(path)).strip()
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
        heard = getattr(recorder, "last", {}) or {}
        log.info("Dictation transcript: %d characters", len(text))  # Never the words.
        if not text:
            if heard.get("voiced", 0) >= 0.5:
                raise NoSpeechDetected(
                    "I heard you but couldn't make out the words. "
                    "Try again, a little closer to the mic."
                )
            raise NoSpeechDetected("I didn't hear anything.")
        return text

    async def _clean(self, text: str, app: str) -> str:
        status = self.service.status
        if status.state != ServiceState.RUNNING or not status.url:
            return text
        token = self.settings.api_token.get_secret_value()
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.post(
                    status.url + "/api/v1/text/dictation",
                    json={"text": text, "app": app[:100]},
                    headers={"Authorization": f"Bearer {token}"},
                )
            return response.json()["result"] if response.status_code == 200 else text
        except Exception:
            return text  # Typing the raw words beats losing them.

    # Main thread -------------------------------------------------------------------------

    def _deliver(self, text: str) -> None:
        name = self.target.get("name") or "the app"
        if not self.can_type():
            # macOS drops the ⌘V Bridge would send; never claim it typed. Keep the words.
            self.copy(text)
            self._hud("error", "Copied — allow Bridge in Accessibility so it can type for you.")
            with contextlib.suppress(Exception):
                self.can_type(prompt=True)
        elif self.front().get("pid") == self.target.get("pid"):
            self.paste(text)
            self._hud("done", f"✓ Typed into {name}")
        else:
            self.copy(text)
            self._hud("done", "You switched apps, so it's copied. Paste it with ⌘V.")
        self.state = DictationState.IDLE

    def _set(self, state: DictationState, look: str, message: str) -> None:
        self.state = state
        self.call(self._hud, look, message)

    def _finish(self, look: str, message: str) -> None:
        def done():
            self.state = DictationState.IDLE
            self._hud(look, message)

        self.call(done)

    def _hud(self, look: str, message: str) -> None:
        if self.hud is not None:
            self.hud.show(look, message)


# The floating status pill ----------------------------------------------------------------

WIDTH, HEIGHT = 340, 44
LOOKS = {"listening": "FF5D73", "working": "49DDFF", "done": GREEN, "error": AMBER}


class PillView(AK.NSView):
    def drawRect_(self, rect):
        bounds = self.bounds()
        color("0C1423", 0.94).setFill()
        rounded(bounds, HEIGHT / 2).fill()
        dot = NSMakeRect(18, HEIGHT / 2 - 5, 10, 10)
        color(LOOKS.get(getattr(self, "look", "working"), TEXT)).setFill()
        AK.NSBezierPath.bezierPathWithOvalInRect_(dot).fill()


class HudActions(NSObject):
    def cancel_(self, sender):
        self.owner.on_cancel()


class DictationHud:
    """A small non-activating pill at the bottom of the screen; it never takes focus, so
    the text still lands in the app you were typing in."""

    def __init__(self, on_cancel):
        self.on_cancel = on_cancel
        self.generation = 0
        self.window = AK.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT),
            AK.NSWindowStyleMaskBorderless | AK.NSWindowStyleMaskNonactivatingPanel,
            AK.NSBackingStoreBuffered,
            False,
        )
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(AK.NSColor.clearColor())
        self.window.setHasShadow_(True)
        self.window.setLevel_(AK.NSStatusWindowLevel)
        self.window.setIgnoresMouseEvents_(False)
        self.window.setReleasedWhenClosed_(False)
        self.window.setCollectionBehavior_(
            AK.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AK.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self.view = PillView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        self.window.setContentView_(self.view)
        self.label = AK.NSTextField.labelWithString_("")
        self.label.setFrame_(NSMakeRect(38, 13, WIDTH - 110, 18))
        self.label.setFont_(AK.NSFont.systemFontOfSize_weight_(13, AK.NSFontWeightMedium))
        self.label.setTextColor_(color(TEXT))
        self.label.setLineBreakMode_(AK.NSLineBreakByTruncatingTail)
        self.view.addSubview_(self.label)
        self.actions = HudActions.alloc().init()
        self.actions.owner = self
        self.cancel = AK.NSButton.buttonWithTitle_target_action_("Cancel", self.actions, "cancel:")
        self.cancel.setFrame_(NSMakeRect(WIDTH - 72, 9, 60, 26))
        self.cancel.setBezelStyle_(AK.NSBezelStyleRounded)
        self.cancel.setControlSize_(AK.NSControlSizeSmall)
        self.view.addSubview_(self.cancel)

    def show(self, look: str, message: str) -> None:
        self.generation += 1
        if look == "hidden":
            self.window.orderOut_(None)
            return
        self.view.look = look
        self.view.setNeedsDisplay_(True)
        self.label.setStringValue_(message)
        self.label.setToolTip_(message)
        self.cancel.setHidden_(look not in ("listening", "working"))
        screen = AK.NSScreen.mainScreen().visibleFrame()
        x = screen.origin.x + (screen.size.width - WIDTH) / 2
        self.window.setFrameOrigin_(NSMakePoint(x, screen.origin.y + 28))
        self.window.orderFrontRegardless()
        if look in ("done", "error"):
            generation = self.generation
            AppHelper.callLater(2.5 if look == "done" else 4, lambda: self._fade(generation))

    def _fade(self, generation: int) -> None:
        if generation == self.generation:  # Nothing newer was shown meanwhile.
            self.window.orderOut_(None)
