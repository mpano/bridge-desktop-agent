"""Which macOS permissions Bridge has, read without asking (no prompts). macOS only."""

from __future__ import annotations

SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_"
ALLOWED, DENIED, NOT_ASKED, UNKNOWN = "allowed", "denied", "not_asked", "unknown"


def _from_code(code: int | None) -> str:
    """EventKit, Contacts and AVFoundation share 0 = not asked, 1–2 = no, 3+ = yes."""
    if code is None:
        return UNKNOWN
    return NOT_ASKED if code == 0 else DENIED if code in (1, 2) else ALLOWED


def _accessibility() -> str:
    import ApplicationServices as AS

    return ALLOWED if AS.AXIsProcessTrusted() else DENIED


def _microphone() -> str:
    import objc

    try:
        objc.loadBundle(
            "AVFoundation", {}, bundle_path="/System/Library/Frameworks/AVFoundation.framework"
        )
        device = objc.lookUpClass("AVCaptureDevice")
        return _from_code(int(device.authorizationStatusForMediaType_("soun")))
    except Exception:
        return UNKNOWN


def _events(kind: int) -> str:
    import EventKit as EK

    return _from_code(int(EK.EKEventStore.authorizationStatusForEntityType_(kind)))


def _contacts() -> str:
    import Contacts as CN

    return _from_code(
        int(CN.CNContactStore.authorizationStatusForEntityType_(CN.CNEntityTypeContacts))
    )


def _screen() -> str:
    import Quartz as Q

    return ALLOWED if Q.CGPreflightScreenCaptureAccess() else NOT_ASKED


def statuses() -> list[dict]:
    checks = [
        (
            "accessibility",
            "Accessibility",
            "Read the text you select and the window you mean by “this”, and type dictation.",
            "Accessibility",
            _accessibility,
        ),
        (
            "microphone",
            "Microphone",
            "Hear you when you talk to Bridge or dictate.",
            "Microphone",
            _microphone,
        ),
        (
            "calendars",
            "Calendars",
            "Plan your day and warn you before meetings.",
            "Calendars",
            lambda: _events(0),
        ),
        ("reminders", "Reminders", "Add and list your reminders.", "Reminders", lambda: _events(1)),
        (
            "contacts",
            "Contacts",
            "Turn “Olivier” into the right email address or number.",
            "Contacts",
            _contacts,
        ),
        (
            "screen",
            "Screen Recording",
            "Optional. Only for apps that hide their text from Accessibility.",
            "ScreenCapture",
            _screen,
        ),
    ]
    result = []
    for key, name, why, pane, check in checks:
        try:
            state = check()
        except Exception:
            state = UNKNOWN
        result.append(
            {"key": key, "name": name, "why": why, "state": state, "settings_url": SETTINGS + pane}
        )
    return result
