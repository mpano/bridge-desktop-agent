"""Phone access: which address and Tailscale user may reach Bridge, pairing, and pushes.

A request counts as "from the phone" only when it arrives on Bridge's private https address
through Tailscale, from your own Tailscale login. Phone requests may use a short list of
screens (Today, Ask, Inbox, approvals); everything else stays on the Mac.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import time
from pathlib import Path

import httpx

from app.phone.webpush import Vapid, allowed_endpoint, encrypt

log = logging.getLogger(__name__)
PAIR_SECONDS = 300
SESSION_HOURS = 24  # Then Face ID again: shorter than the Mac's "keep me signed in".
AWAY_SECONDS = 300
MAX_SUBSCRIPTIONS = 5
NOTIFY_MODES = ("away", "always")

# What the phone may use. Everything else answers "do this on your Mac".
PHONE_PATHS = {
    ("GET", "/"),
    ("GET", "/sw.js"),
    ("GET", "/manifest.webmanifest"),
    ("GET", "/api/v1/auth/status"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/auth/passkey-login/options"),
    ("POST", "/api/v1/auth/passkey-login"),
    ("POST", "/api/v1/auth/passkeys/options"),
    ("POST", "/api/v1/auth/passkeys"),
    ("POST", "/api/v1/phone/pair"),
    ("GET", "/api/v1/phone/me"),
    ("POST", "/api/v1/phone/push"),
    ("POST", "/api/v1/phone/push/remove"),
    ("POST", "/api/v1/phone/push/test"),
    ("GET", "/api/v1/today"),
    ("POST", "/api/v1/today/plan"),
    ("POST", "/api/v1/today/plan/apply"),
    ("GET", "/api/v1/inbox"),
    ("GET", "/api/v1/inbox/summary"),
    ("POST", "/api/v1/inbox/triage"),
    ("POST", "/api/v1/inbox/open"),
    ("POST", "/api/v1/inbox/draft"),
    ("POST", "/api/v1/inbox/send"),
    ("POST", "/api/v1/followups/cancel"),
    ("GET", "/api/v1/approvals"),
    ("GET", "/api/v1/brief"),
    ("POST", "/api/v1/brief/move"),
    ("GET", "/api/v1/commitments"),
    ("POST", "/api/v1/commitments/scan"),
    ("POST", "/api/v1/commitments/update"),
    ("POST", "/api/v1/commitments/draft"),
    ("POST", "/api/v1/commitments/send"),
    ("GET", "/api/v1/tasks"),
    ("POST", "/api/v1/tasks"),
    ("POST", "/api/v1/tasks/confirm"),
    ("GET", "/api/v1/conversation"),
    ("POST", "/api/v1/conversation/reset"),
    ("GET", "/api/v1/chats"),
    ("POST", "/api/v1/chats/open"),
    ("GET", "/api/v1/focus"),
    ("POST", "/api/v1/focus/start"),
    ("POST", "/api/v1/focus/stop"),
}
PHONE_PREFIXES = (("GET", "/ui/"), ("GET", "/api/v1/tasks/"), ("POST", "/api/v1/tasks/"))
# Tools that read this Mac's screen, clipboard or run commands: only when you're at the Mac.
MAC_ONLY_TOOLS = frozenset(
    {
        "screen_context",
        "take_screenshot",
        "read_clipboard",
        "copy_to_clipboard",
        "run_terminal_command",
        "chrome_read_page",
        "set_preferences",
        "proactive_settings",
        "prune_workflow_records",
    }
)


def phone_may_use(method: str, path: str) -> bool:
    method = "GET" if method == "HEAD" else method
    if (method, path) in PHONE_PATHS:
        return True
    return any(method == m and path.startswith(p) for m, p in PHONE_PREFIXES)


def mac_is_away() -> bool:
    """True when the screen is locked or nobody has touched the Mac for a few minutes."""
    try:
        import Quartz

        session = Quartz.CGSessionCopyCurrentDictionary() or {}
        if session.get("CGSSessionScreenIsLocked"):
            return True
        idle = Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType
        )
        return idle >= AWAY_SECONDS
    except Exception:
        return True  # Can't tell: better a duplicate than a missed approval.


class PhoneAccess:
    def __init__(self, path: Path | str, clock=time.time, away=mac_is_away):
        self.path, self.clock, self.away = str(path), clock, away
        self.tickets: dict[str, float] = {}
        self._who: tuple[str, str] | None = None  # (host, login), read on every request.
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS phone_settings (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS phone_push (
                    endpoint TEXT PRIMARY KEY, p256dh TEXT NOT NULL, auth TEXT NOT NULL,
                    created REAL NOT NULL);
                """
            )

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    def _get(self, key: str, default: str = "") -> str:
        with self._db() as db:
            row = db.execute("SELECT value FROM phone_settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def _set(self, **values: str | None) -> None:
        with self._db() as db:
            for key, value in values.items():
                if value is None:
                    db.execute("DELETE FROM phone_settings WHERE key = ?", (key,))
                else:
                    db.execute(
                        "INSERT INTO phone_settings VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, value),
                    )

    # Who may reach Bridge -------------------------------------------------------------------

    def _identity(self) -> tuple[str, str]:
        if self._who is None:
            self._who = (self._get("host"), self._get("login"))
        return self._who

    @property
    def host(self) -> str:
        return self._identity()[0]

    @property
    def login(self) -> str:
        return self._identity()[1]

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.login)

    @property
    def origin(self) -> str:
        return f"https://{self.host}" if self.host else ""

    def configure(self, host: str, login: str) -> None:
        self._set(host=host.lower(), login=login)
        self._who = None

    def disable(self) -> None:
        self._set(host=None, login=None)
        self._who = None
        self.tickets.clear()
        with self._db() as db:
            db.execute("DELETE FROM phone_push")

    def is_phone_host(self, request) -> bool:
        host = (request.headers.get("host") or "").split(":")[0].lower()
        return bool(host) and host == self.host

    def is_phone(self, request) -> bool:
        """Arrived through Tailscale, on Bridge's address, from your own Tailscale login."""
        return (
            self.enabled
            and self.is_phone_host(request)
            and request.headers.get("tailscale-user-login", "") == self.login
        )

    # Pairing: the QR code on the Mac ---------------------------------------------------------

    def pair_ticket(self) -> str:
        now = self.clock()
        self.tickets = {t: exp for t, exp in self.tickets.items() if exp > now}
        ticket = secrets.token_urlsafe(24)
        self.tickets[ticket] = now + PAIR_SECONDS
        return ticket

    def redeem(self, ticket: str) -> bool:
        expires = self.tickets.pop(ticket, 0)
        return expires > self.clock()

    # Notifications on the phone ---------------------------------------------------------------

    @property
    def notify_mode(self) -> str:
        mode = self._get("notify", "away")
        return mode if mode in NOTIFY_MODES else "away"

    def set_notify_mode(self, mode: str) -> None:
        if mode not in NOTIFY_MODES:
            raise ValueError("Choose when to notify your phone.")
        self._set(notify=mode)

    def vapid(self) -> Vapid:
        pem = self._get("vapid")
        if pem:
            return Vapid(pem.encode())
        key = Vapid()
        self._set(vapid=key.pem().decode())
        return key

    def subscriptions(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT endpoint, p256dh, auth FROM phone_push ORDER BY created"
            ).fetchall()
        return [{"endpoint": r[0], "p256dh": r[1], "auth": r[2]} for r in rows]

    def subscribe(self, endpoint: str, p256dh: str, auth: str) -> None:
        if not allowed_endpoint(endpoint):
            raise ValueError("That isn't a phone notification service Bridge knows.")
        with self._db() as db:
            db.execute(
                "INSERT INTO phone_push VALUES (?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE "
                "SET p256dh = excluded.p256dh, auth = excluded.auth",
                (endpoint, p256dh, auth, self.clock()),
            )
            # Keep the newest few: each reinstall of the home-screen app subscribes again.
            db.execute(
                "DELETE FROM phone_push WHERE endpoint NOT IN "
                "(SELECT endpoint FROM phone_push ORDER BY created DESC LIMIT ?)",
                (MAX_SUBSCRIPTIONS,),
            )

    def unsubscribe(self, endpoint: str | None = None) -> None:
        with self._db() as db:
            if endpoint is None:
                db.execute("DELETE FROM phone_push")
            else:
                db.execute("DELETE FROM phone_push WHERE endpoint = ?", (endpoint,))

    async def notify(self, title: str, body: str, view: str = "today", *, force=False) -> int:
        """Push to the phone (when you're away from the Mac, unless set to always). Never raises."""
        try:
            targets = self.subscriptions() if self.enabled else []
            if not targets or (not force and self.notify_mode == "away" and not self.away()):
                return 0
            vapid = self.vapid()
            message = json.dumps(
                {"title": title[:100], "body": " ".join(body.split())[:400], "view": view}
            ).encode()
            sent = 0
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                for target in targets:
                    sent += await self._send(client, vapid, target, message)
            return sent
        except Exception:
            log.warning("Couldn't notify the phone.", exc_info=True)
            return 0

    async def _send(self, client, vapid: Vapid, target: dict, message: bytes) -> int:
        endpoint = target["endpoint"]
        if not allowed_endpoint(endpoint):
            self.unsubscribe(endpoint)
            return 0
        response = await client.post(
            endpoint,
            content=encrypt(message, target["p256dh"], target["auth"]),
            headers={
                "Authorization": vapid.header(endpoint, self.origin, self.clock()),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(4 * 3600),
                "Urgency": "high",
            },
        )
        if response.status_code in (404, 410):
            self.unsubscribe(endpoint)  # The phone app was removed or turned notifications off.
            return 0
        if response.status_code >= 300:
            log.warning("Phone push refused: HTTP %s %s", response.status_code, response.text[:200])
            return 0
        return 1
