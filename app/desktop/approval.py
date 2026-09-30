"""A native, explicit review of the exact tool arguments. No speech approval path."""

import AppKit as AK
from Foundation import NSMakeRect

from app.desktop.voice import ApprovalView


class ApprovalSheet:
    def __init__(self, view: ApprovalView, on_decision):
        self.view = view
        self.on_decision = on_decision
        self.alert = AK.NSAlert.alloc().init()
        self.alert.setMessageText_(f"Allow {view.action}?")
        self.alert.setInformativeText_(
            view.message + "\n\nReview the full arguments below. "
            "Approval applies only to this action and expires automatically."
        )
        # The first/default button declines. Return never implicitly approves.
        self.alert.addButtonWithTitle_("Decline")
        approve = self.alert.addButtonWithTitle_("Approve once")
        approve.setKeyEquivalent_("")
        scroll = AK.NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 480, 240))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(AK.NSBezelBorder)
        text = AK.NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 460, 240))
        text.setEditable_(False)
        text.setSelectable_(True)
        text.setRichText_(False)
        text.setFont_(AK.NSFont.monospacedSystemFontOfSize_weight_(12, AK.NSFontWeightRegular))
        text.setString_(view.arguments)
        text.setVerticallyResizable_(True)
        text.setHorizontallyResizable_(False)
        text.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(text)
        self.arguments_view = text
        self.alert.setAccessoryView_(scroll)

    def show(self, window):
        self.alert.beginSheetModalForWindow_completionHandler_(window, self.decide)

    def decide(self, response):
        self.on_decision(self.view.review_id, response == AK.NSAlertSecondButtonReturn)
