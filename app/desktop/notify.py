"""Bridge's own notifications: posted by Bridge.app (its icon and name), and clicking one
opens the Bridge window on the screen it's about (Today, Inbox, Ask…). macOS only.

Set up once on the main thread at launch. Text is plain: nothing in it is interpreted.
"""

from __future__ import annotations

import uuid

import UserNotifications as UN
from Foundation import NSObject

VIEWS = {"today", "chat", "inbox", "automations", "connections", "memory", "settings"}


class NotificationDelegate(NSObject):
    # Clicked: open the window on the screen the notification is about.
    def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
        self, center, response, handler
    ):
        info = response.notification().request().content().userInfo()
        view = str(info.get("view", "today")) if info else "today"
        self.on_click(view if view in VIEWS else "today")
        handler()

    # Show the banner even when Bridge is the app in front.
    def userNotificationCenter_willPresentNotification_withCompletionHandler_(
        self, center, notification, handler
    ):
        handler(
            UN.UNNotificationPresentationOptionBanner
            | UN.UNNotificationPresentationOptionList
            | UN.UNNotificationPresentationOptionSound
        )


class NativeNotifier:
    def __init__(self, on_click):
        self.center = UN.UNUserNotificationCenter.currentNotificationCenter()
        self.delegate = NotificationDelegate.alloc().init()
        self.delegate.on_click = on_click
        self.center.setDelegate_(self.delegate)
        # macOS asks once ("Bridge would like to send notifications").
        self.center.requestAuthorizationWithOptions_completionHandler_(
            UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound,
            lambda granted, error: None,
        )

    def post(self, title: str, message: str, view: str = "today") -> None:
        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(" ".join(title.split())[:100])
        content.setBody_(" ".join(message.split())[:400])
        content.setSound_(UN.UNNotificationSound.defaultSound())
        content.setUserInfo_({"view": view if view in VIEWS else "today"})
        request = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
            str(uuid.uuid4()), content, None
        )
        self.center.addNotificationRequest_withCompletionHandler_(request, None)
