"""The Bridge window: the dashboard in a native Mac window instead of a browser tab.

It signs in with the same single-use launch ticket the menu bar uses, and keeps its
session in WebKit's own store. Only Bridge's local pages load inside it; anything else
(a provider's sign-in page, a link in an email) opens in the default browser, where
Google and Slack allow sign-in. AppKit, main thread only.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

import AppKit as AK
import WebKit as WK
from Foundation import NSURL, NSMakeRect, NSObject, NSURLRequest

WIDTH, HEIGHT = 1280, 820
# Links that leave Bridge: the web in the default browser, mail, and System Settings panes.
EXTERNAL = ("https://", "http://", "mailto:", "x-apple.systempreferences:")
# Sign in again when reopened after this long, well inside the dashboard session's life.
FRESH_SECONDS = 4 * 3600
STARTING = """<!doctype html><meta charset="utf-8"><style>
:root{color-scheme:light dark}body{margin:0;height:100vh;display:flex;align-items:center;
justify-content:center;font:15px -apple-system,sans-serif;background:#F4F4F1;color:#5B5E66}
@media (prefers-color-scheme:dark){body{background:#141519;color:#A9ABB2}}</style>
<p>Starting Bridge…</p>"""


def is_local(url: str, port: int) -> bool:
    parts = urlsplit(url)
    if parts.scheme in {"about", "data", "blob"}:
        return True
    return (
        parts.scheme == "http"
        and parts.hostname in {"localhost", "127.0.0.1"}
        and (parts.port or 80) == port
    )


class WindowDelegate(NSObject):
    # Navigation: keep Bridge inside, send everything else to the browser.
    def webView_decidePolicyForNavigationAction_decisionHandler_(self, web, action, handler):
        url = action.request().URL()
        text = str(url.absoluteString()) if url is not None else ""
        if is_local(text, self.owner.port):
            handler(WK.WKNavigationActionPolicyAllow)
            return
        handler(WK.WKNavigationActionPolicyCancel)
        if text.startswith(EXTERNAL):
            AK.NSWorkspace.sharedWorkspace().openURL_(url)

    # target="_blank" and window.open: open in the browser, never a second web view.
    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
        self, web, configuration, action, features
    ):
        url = action.request().URL()
        if url is not None and str(url.absoluteString()).startswith(EXTERNAL):
            AK.NSWorkspace.sharedWorkspace().openURL_(url)
        return None

    # window.alert / window.confirm as native alerts (WebKit drops them otherwise).
    def webView_runJavaScriptAlertPanelWithMessage_initiatedByFrame_completionHandler_(
        self, web, message, frame, handler
    ):
        self.owner.alert(str(message), buttons=("OK",))
        handler()

    def webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(
        self, web, message, frame, handler
    ):
        handler(self.owner.alert(str(message), buttons=("OK", "Cancel")) == 0)

    def webView_didFailProvisionalNavigation_withError_(self, web, navigation, error):
        self.owner.loaded = False

    def webViewWebContentProcessDidTerminate_(self, web):
        self.owner.loaded = False
        self.owner.load()

    # Closing the window keeps Bridge running in the menu bar.
    def windowWillClose_(self, notification):
        self.owner.closed()

    def windowDidMiniaturize_(self, notification):
        self.owner.controller.update_dock()


class BridgeWindow:
    def __init__(self, controller, service):
        self.controller, self.service = controller, service
        self.port = service.port
        self.loaded = False
        self.loaded_at = 0.0
        self.delegate = WindowDelegate.alloc().init()
        self.delegate.owner = self
        style = (
            AK.NSWindowStyleMaskTitled
            | AK.NSWindowStyleMaskClosable
            | AK.NSWindowStyleMaskMiniaturizable
            | AK.NSWindowStyleMaskResizable
            | AK.NSWindowStyleMaskFullSizeContentView
        )
        self.window = AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), style, AK.NSBackingStoreBuffered, False
        )
        self.window.setTitle_("Bridge")
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(AK.NSWindowTitleHidden)
        self.window.setMinSize_((960, 640))
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self.delegate)
        self.window.setTabbingMode_(AK.NSWindowTabbingModeDisallowed)
        if not self.window.setFrameUsingName_("BridgeMain"):
            self.window.center()
        self.window.setFrameAutosaveName_("BridgeMain")

        config = WK.WKWebViewConfiguration.alloc().init()
        config.setWebsiteDataStore_(WK.WKWebsiteDataStore.defaultDataStore())
        # Settings asks the app for microphone things (the "Hey Bridge" switch, training).
        from app.desktop.floating import PageMessages

        self.messages = PageMessages.alloc().init()
        self.messages.owner = self
        config.userContentController().addScriptMessageHandler_name_(self.messages, "bridge")
        self._voice_sent = None
        self.web = WK.WKWebView.alloc().initWithFrame_configuration_(
            self.window.contentView().bounds(), config
        )
        self.web.setAutoresizingMask_(AK.NSViewWidthSizable | AK.NSViewHeightSizable)
        self.web.setNavigationDelegate_(self.delegate)
        self.web.setUIDelegate_(self.delegate)
        self.web.setAllowsBackForwardNavigationGestures_(False)
        self.window.contentView().addSubview_(self.web)

    @property
    def visible(self) -> bool:
        return bool(self.window.isVisible() or self.window.isMiniaturized())

    def load(self, view: str | None = None, auto: bool = False) -> bool:
        """Sign in with a fresh launch ticket. False while the service is starting."""
        url = self.service.dashboard_url()
        if url is not None and auto:
            url += "&auto=1"  # The page asked for this; it shows a normal login if it fails.
        if url is not None and view and view.isalpha():
            url += f"&view={view}"  # The page opens on that screen once signed in.
        if url is None:
            self.web.loadHTMLString_baseURL_(STARTING, None)
            return False
        self.web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
        self.loaded, self.loaded_at = True, time.monotonic()
        return True

    def show(self, view: str | None = None) -> None:
        if self.controller.quitting:
            return
        stale = time.monotonic() - self.loaded_at > FRESH_SECONDS
        opening = not self.loaded or (stale and not self.visible)
        if opening:
            self.load(view)
        if self.window.isMiniaturized():
            self.window.deminiaturize_(None)
        self.window.makeKeyAndOrderFront_(None)
        self.controller.update_dock()
        AK.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if view and self.loaded and not opening:
            # A screen named by the panel or command bar ("today", "chat", "inbox", …).
            self.web.evaluateJavaScript_completionHandler_(
                f"window.BridgeUI && window.BridgeUI.view({view!r})", None
            )

    def retry(self) -> None:
        """Called by the menu bar's refresh: load once the service is up."""
        if self.visible and not self.loaded:
            self.load()

    def closed(self) -> None:
        self.controller.update_dock(closing=self)

    def on_message(self, message: dict) -> None:
        """A fixed list of actions from the page; anything else is ignored."""
        action = message.get("action")
        if action == "wake_on":
            self.controller.start_voice()
        elif action == "wake_off":
            self.controller.stop_voice()
        elif action == "train_wake":
            self.controller.train_voice()
        elif action == "signin":
            # The session expired while the window was open: sign in again, at most
            # every 10 seconds so a failure can't turn into a reload loop.
            now = time.monotonic()
            if now - getattr(self, "_signin_at", 0.0) > 10:
                self._signin_at = now
                self.load(auto=True)
        self._voice_sent = None  # Send the new state on the next refresh.

    def push_voice(self, voice) -> None:
        """Called by the menu bar's refresh: tell the page about the microphone."""
        if not self.loaded or voice is None:
            return
        status = voice.status
        state = (status.state.value, status.message, voice.wake_enabled, voice.wake_model_ready)
        if state != self._voice_sent:
            self._voice_sent = state
            payload = json.dumps(
                {"state": state[0], "message": state[1], "listening": state[2], "ready": state[3]}
            )
            self.web.evaluateJavaScript_completionHandler_(
                f"window.BridgeNative && window.BridgeNative.voice({payload})", None
            )

    def alert(self, message: str, buttons=("OK",)) -> int:
        alert = AK.NSAlert.alloc().init()
        alert.setMessageText_(message)
        for title in buttons:
            alert.addButtonWithTitle_(title)
        return int(alert.runModal()) - AK.NSAlertFirstButtonReturn
