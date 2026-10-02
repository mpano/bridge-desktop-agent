"""Floating Bridge pages: the compact menu bar panel and the ⌥Space command bar.

Each is a borderless, non-activating panel (like Spotlight) holding a small page of the
dashboard, so it shares the window's design and dark mode while the app you were in stays
active. The page talks back through one message channel with a fixed list of actions
(speak, stop, open the window, hide, resize); anything else is ignored. AppKit, main thread.
"""

from __future__ import annotations

import json
import time

import AppKit as AK
import WebKit as WK
from Foundation import NSURL, NSMakePoint, NSMakeRect, NSObject, NSURLRequest

from app.desktop.panel import PanelActions
from app.desktop.service import ServiceState
from app.desktop.window import EXTERNAL, is_local

FRESH_SECONDS = 4 * 3600


class KeyPanel(AK.NSPanel):
    """A borderless panel that can still take typing."""

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return False


class PageMessages(NSObject):
    def userContentController_didReceiveScriptMessage_(self, controller, message):
        origin = message.frameInfo().securityOrigin()
        if (
            str(origin.host()) not in {"localhost", "127.0.0.1"}
            or int(origin.port()) != self.owner.port
        ):
            return
        body = message.body()
        if isinstance(body, dict) or hasattr(body, "objectForKey_"):
            self.owner.on_message({str(key): body[key] for key in body})


class PageDelegate(NSObject):
    def webView_decidePolicyForNavigationAction_decisionHandler_(self, web, action, handler):
        url = action.request().URL()
        text = str(url.absoluteString()) if url is not None else ""
        if is_local(text, self.owner.port):
            handler(WK.WKNavigationActionPolicyAllow)
            return
        handler(WK.WKNavigationActionPolicyCancel)
        if text.startswith(EXTERNAL):
            AK.NSWorkspace.sharedWorkspace().openURL_(url)

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
        self, web, configuration, action, features
    ):
        url = action.request().URL()
        if url is not None and str(url.absoluteString()).startswith(EXTERNAL):
            AK.NSWorkspace.sharedWorkspace().openURL_(url)
        return None

    def webViewWebContentProcessDidTerminate_(self, web):
        self.owner.loaded = False

    def windowDidResignKey_(self, notification):
        self.owner.resigned()


class FloatingPage:
    PAGE = ""
    WIDTH, HEIGHT = 360, 470

    def __init__(self, controller, service):
        self.controller, self.service = controller, service
        self.port = service.port
        self.loaded, self.loaded_at = False, 0.0
        self.delegate = PageDelegate.alloc().init()
        self.delegate.owner = self
        self.messages = PageMessages.alloc().init()
        self.messages.owner = self
        self.window = KeyPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.WIDTH, self.HEIGHT),
            AK.NSWindowStyleMaskBorderless | AK.NSWindowStyleMaskNonactivatingPanel,
            AK.NSBackingStoreBuffered,
            False,
        )
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(AK.NSColor.clearColor())
        self.window.setHasShadow_(True)
        self.window.setLevel_(AK.NSPopUpMenuWindowLevel)
        self.window.setReleasedWhenClosed_(False)
        self.window.setHidesOnDeactivate_(False)
        self.window.setCollectionBehavior_(
            AK.NSWindowCollectionBehaviorMoveToActiveSpace
            | AK.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self.window.setDelegate_(self.delegate)
        config = WK.WKWebViewConfiguration.alloc().init()
        config.setWebsiteDataStore_(WK.WKWebsiteDataStore.defaultDataStore())
        config.userContentController().addScriptMessageHandler_name_(self.messages, "bridge")
        self.web = WK.WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, self.WIDTH, self.HEIGHT), config
        )
        self.web.setValue_forKey_(False, "drawsBackground")  # The page draws its own card.
        self.web.setAutoresizingMask_(AK.NSViewWidthSizable | AK.NSViewHeightSizable)
        self.web.setNavigationDelegate_(self.delegate)
        self.web.setUIDelegate_(self.delegate)
        content = self.window.contentView()
        content.setWantsLayer_(True)
        content.layer().setCornerRadius_(14)
        content.layer().setMasksToBounds_(True)
        content.addSubview_(self.web)
        self.web.setFrame_(content.bounds())

    # Loading ------------------------------------------------------------------------------

    def load(self) -> bool:
        status = self.service.status
        if status.state != ServiceState.RUNNING or not status.url:
            return False
        url = f"http://localhost:{self.port}/ui/{self.PAGE}#launch={self.service.tickets.issue()}"
        self.web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
        self.loaded, self.loaded_at = True, time.monotonic()
        return True

    def preload(self) -> None:
        """Load in the background as soon as the service is up, so it opens instantly."""
        if not self.loaded or time.monotonic() - self.loaded_at > FRESH_SECONDS:
            if not self.window.isVisible():
                self.load()

    def send(self, script: str) -> None:
        if self.loaded:
            self.web.evaluateJavaScript_completionHandler_(script, None)

    # Showing ------------------------------------------------------------------------------

    def place(self) -> None:
        raise NotImplementedError

    @property
    def visible(self) -> bool:
        return bool(self.window.isVisible())

    def show(self) -> None:
        if self.controller.quitting:
            return
        if not self.loaded:
            self.load()
        self.place()
        self.window.makeKeyAndOrderFront_(None)
        self.window.makeFirstResponder_(self.web)
        self.send("window.BridgeMini && window.BridgeMini.shown()")

    def hide(self) -> None:
        self.window.orderOut_(None)

    def toggle(self) -> None:
        if self.window.isVisible():
            self.hide()
        else:
            self.show()

    def resigned(self) -> None:
        self.hide()  # Clicking anywhere else closes it, like a menu.

    # Messages from the page -----------------------------------------------------------------

    def on_message(self, message: dict) -> None:
        action = message.get("action")
        if action == "hide":
            self.hide()
        elif action == "open":
            self.hide()
            view = str(message.get("view") or "")
            window = getattr(self.controller, "window", None)
            if window is not None:
                window.show(view if view.isalpha() else None)
            else:
                self.controller.open_dashboard()
        elif action == "speak":
            voice = getattr(self.controller, "voice", None)
            if voice is not None and not voice.active:
                self.controller.speak_once()
        elif action == "stop":
            voice = getattr(self.controller, "voice", None)
            if voice is not None:
                voice.stop()


class MenuPanel(FloatingPage):
    """The compact panel under the menu bar icon."""

    PAGE = "panel.html"
    WIDTH, HEIGHT = 360, 476
    counts_for_dock = False

    def __init__(self, controller, service):
        super().__init__(controller, service)
        self.actions = PanelActions.alloc().init()  # Menu bar clicks, main menu, reopen.
        self.actions.owner = self
        self.status_item = None
        self.menu = None
        self.dock_icon = None
        self._voice_sent = None

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
        if self.status_item is None or self.menu is None:
            return
        self.controller.refresh(None)
        self.status_item.setMenu_(self.menu)
        self.status_item.button().performClick_(None)
        self.status_item.setMenu_(None)

    def place(self) -> None:
        if self.status_item is None:
            self.window.center()
            return
        button = self.status_item.button()
        anchor = button.window().convertRectToScreen_(button.frame())
        screen = (button.window().screen() or AK.NSScreen.mainScreen()).visibleFrame()
        x = min(
            max(anchor.origin.x + anchor.size.width / 2 - self.WIDTH / 2, screen.origin.x + 8),
            screen.origin.x + screen.size.width - self.WIDTH - 8,
        )
        self.window.setFrameOrigin_(NSMakePoint(x, anchor.origin.y - self.HEIGHT - 6))

    def set_dock_visible(self, visible: bool) -> None:
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

    def dock_off(self):
        self.controller.update_dock(closing=self)

    def refresh(self) -> None:
        """Called 4×/s by the menu bar: load when ready and pass the voice state along."""
        self.preload()
        voice = getattr(self.controller, "voice", None)
        if voice is None:
            return
        status = voice.status
        state = (status.state.value, status.message, status.transcript or "")
        if state != self._voice_sent and self.loaded:
            self._voice_sent = state
            payload = json.dumps({"state": state[0], "message": state[1], "transcript": state[2]})
            self.send(f"window.BridgePanel && window.BridgePanel.voice({payload})")


class CommandBar(FloatingPage):
    """⌥Space: ask from anywhere, Spotlight-style."""

    PAGE = "command.html"
    WIDTH, HEIGHT = 640, 84

    def place(self) -> None:
        screen = AK.NSScreen.mainScreen().visibleFrame()
        x = screen.origin.x + (screen.size.width - self.WIDTH) / 2
        top = screen.origin.y + screen.size.height * 0.78
        frame = self.window.frame()
        self.window.setFrameOrigin_(NSMakePoint(x, top - frame.size.height))

    def on_message(self, message: dict) -> None:
        if message.get("action") == "size":
            height = max(60, min(int(message.get("height") or 84), 560))
            frame = self.window.frame()
            top = frame.origin.y + frame.size.height
            self.window.setFrame_display_(
                NSMakeRect(frame.origin.x, top - height, self.WIDTH, height), True
            )
            return
        super().on_message(message)
