"""Menu bar clicks, the app menu and "open Bridge again" handling, shared by the panel.

AppKit stays on the launcher's main thread.
"""

from __future__ import annotations

import AppKit as AK
import objc
from Foundation import NSObject


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

    def windowWillClose_(self, notification):
        # The red close button: Bridge keeps running in the menu bar, without a Dock icon.
        self.owner.dock_off()

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
        # Opening Bridge.app again (Finder, Spotlight, Dock) while it runs shows the window.
        open_window = getattr(self.owner.controller, "open_dashboard", None)
        if getattr(self.owner.controller, "window", None) is not None and open_window:
            open_window()
        else:
            self.owner.show()

    def listenForReopen(self):
        AK.NSAppleEventManager.sharedAppleEventManager().setEventHandler_andSelector_forEventClass_andEventID_(  # noqa: E501
            self, "handleReopen:withReplyEvent:", fourcc(b"aevt"), fourcc(b"rapp")
        )


def fourcc(code: bytes) -> int:
    return int.from_bytes(code, "big")


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
