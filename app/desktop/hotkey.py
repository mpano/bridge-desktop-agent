"""Register one macOS hotkey. This does not monitor or log keyboard input."""

import ctypes as ct
import sys
import threading


class EventType(ct.Structure):
    _fields_ = [("event_class", ct.c_uint32), ("kind", ct.c_uint32)]


class HotKeyID(ct.Structure):
    _fields_ = [("signature", ct.c_uint32), ("identifier", ct.c_uint32)]


class GlobalVoiceShortcut:
    LABEL = "⌘⇧Space"

    def __init__(self, callback, library=None):
        self.callback = callback
        self.library = library
        self.key_ref = ct.c_void_p()
        self.handler_ref = ct.c_void_p()
        self.handler = None
        self.status = "Shortcut off"

    def start(self) -> bool:
        if self.key_ref.value:
            return True
        if sys.platform != "darwin" or threading.current_thread() is not threading.main_thread():
            self.status = "Shortcut requires the macOS main thread"
            return False
        try:
            lib = self.library or ct.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
            self.library = lib
            callback_type = ct.CFUNCTYPE(ct.c_int32, ct.c_void_p, ct.c_void_p, ct.c_void_p)
            lib.GetApplicationEventTarget.argtypes = []
            lib.GetApplicationEventTarget.restype = ct.c_void_p
            lib.InstallEventHandler.argtypes = [
                ct.c_void_p,
                callback_type,
                ct.c_uint32,
                ct.POINTER(EventType),
                ct.c_void_p,
                ct.POINTER(ct.c_void_p),
            ]
            lib.InstallEventHandler.restype = ct.c_int32
            lib.RegisterEventHotKey.argtypes = [
                ct.c_uint32,
                ct.c_uint32,
                HotKeyID,
                ct.c_void_p,
                ct.c_uint32,
                ct.POINTER(ct.c_void_p),
            ]
            lib.RegisterEventHotKey.restype = ct.c_int32
            lib.GetEventParameter.argtypes = [
                ct.c_void_p,
                ct.c_uint32,
                ct.c_uint32,
                ct.c_void_p,
                ct.c_uint32,
                ct.c_void_p,
                ct.c_void_p,
            ]
            lib.GetEventParameter.restype = ct.c_int32
            lib.UnregisterEventHotKey.argtypes = [ct.c_void_p]
            lib.UnregisterEventHotKey.restype = ct.c_int32
            lib.RemoveEventHandler.argtypes = [ct.c_void_p]
            lib.RemoveEventHandler.restype = ct.c_int32
            identifier = HotKeyID(int.from_bytes(b"Brdg", "big"), 1)

            def handle(_next, event, _data):
                value = HotKeyID()
                code = lib.GetEventParameter(
                    event,
                    int.from_bytes(b"----", "big"),
                    int.from_bytes(b"hkid", "big"),
                    None,
                    ct.sizeof(value),
                    None,
                    ct.byref(value),
                )
                if code or value.signature != identifier.signature or value.identifier != 1:
                    return -9874  # eventNotHandledErr
                try:
                    self.callback()
                except Exception:
                    self.status = "Shortcut could not start voice; use the panel"
                return 0

            self.handler = callback_type(handle)  # Keep callback alive until handler removal.
            target = lib.GetApplicationEventTarget()
            event_type = EventType(int.from_bytes(b"keyb", "big"), 6)
            if lib.InstallEventHandler(
                target, self.handler, 1, ct.byref(event_type), None, ct.byref(self.handler_ref)
            ):
                raise RuntimeError("Could not install hotkey handler")
            # Physical Space key (49), Command (256) + Shift (512).
            if lib.RegisterEventHotKey(
                49, 256 | 512, identifier, target, 0, ct.byref(self.key_ref)
            ):
                raise RuntimeError("Shortcut unavailable")
        except (OSError, AttributeError, RuntimeError):
            self.close()
            self.status = "Shortcut unavailable — use Speak now"
            return False
        self.status = f"{self.LABEL} to speak"
        return True

    def close(self):
        if self.library is not None:
            if self.key_ref.value:
                self.library.UnregisterEventHotKey(self.key_ref)
                self.key_ref = ct.c_void_p()
            if self.handler_ref.value:
                self.library.RemoveEventHandler(self.handler_ref)
                self.handler_ref = ct.c_void_p()
        self.handler = None
