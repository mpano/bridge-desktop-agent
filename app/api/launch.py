"""Single-use dashboard launch tickets issued by the owning menu-bar process.

The menu bar already holds API_TOKEN. Rather than asking the user to paste it, it opens
the dashboard with a random ticket in the URL fragment (never sent in HTTP requests).
The page exchanges the ticket once for the token, which it keeps in memory only.
"""

import secrets
import threading
import time
from collections.abc import Callable


class LaunchTickets:
    TTL_SECONDS = 60
    MAX_OUTSTANDING = 8

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._guard = threading.Lock()
        self._tickets: dict[str, float] = {}

    def issue(self) -> str:
        ticket = secrets.token_urlsafe(32)
        with self._guard:
            self._expire()
            while len(self._tickets) >= self.MAX_OUTSTANDING:
                self._tickets.pop(next(iter(self._tickets)))
            self._tickets[ticket] = self._clock() + self.TTL_SECONDS
        return ticket

    def redeem(self, ticket: str) -> bool:
        if not ticket.isascii():
            return False
        with self._guard:
            self._expire()
            matched = next(
                (known for known in self._tickets if secrets.compare_digest(known, ticket)),
                None,
            )
            if matched is None:
                return False
            del self._tickets[matched]
            return True

    def _expire(self) -> None:
        now = self._clock()
        for ticket in [key for key, deadline in self._tickets.items() if deadline <= now]:
            del self._tickets[ticket]
