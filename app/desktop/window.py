"""The Bridge window: the dashboard in a native Mac window instead of a browser tab.

It signs in with the same single-use launch ticket the menu bar uses, and keeps its
session in WebKit's own store. Only Bridge's local pages load inside it; anything else
(a provider's sign-in page, a link in an email) opens in the default browser, where
Google and Slack allow sign-in. AppKit, main thread only.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import AppKit as AK
import WebKit as WK
from Foundation import NSURL, NSMakeRect, NSObject, NSURLRequest

WIDTH, HEIGHT = 1280, 820
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
        if text.startswith(("https://", "http://", "mailto:")):
            AK.NSWorkspace.sharedWorkspace().openURL_(url)

    # target="_blank" and window.open: open in the browser, never a second web view.
    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
        self, web, configuration, action, features
    ):
        url = action.request().URL()
        if url is not None and str(url.scheme()) in {"https", "http", "mailto"}:
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

    def load(self) -> bool:
        """Sign in with a fresh launch ticket. False while the service is starting."""
        url = self.service.dashboard_url()
        if url is None:
            self.web.loadHTMLString_baseURL_(STARTING, None)
            return False
        self.web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
        self.loaded, self.loaded_at = True, time.monotonic()
        return True

    def show(self) -> None:
        if self.controller.quitting:
            return
        stale = time.monotonic() - self.loaded_at > FRESH_SECONDS
        if not self.loaded or (stale and not self.visible):
            self.load()
        if self.window.isMiniaturized():
            self.window.deminiaturize_(None)
        self.window.makeKeyAndOrderFront_(None)
        self.controller.update_dock()
        AK.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def retry(self) -> None:
        """Called by the menu bar's refresh: load once the service is up."""
        if self.visible and not self.loaded:
            self.load()

    def closed(self) -> None:
        self.controller.update_dock(closing=self)

    def alert(self, message: str, buttons=("OK",)) -> int:
        alert = AK.NSAlert.alloc().init()
        alert.setMessageText_(message)
        for title in buttons:
            alert.addButtonWithTitle_(title)
        return int(alert.runModal()) - AK.NSAlertFirstButtonReturn
