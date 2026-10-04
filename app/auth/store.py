"""Owner account, linked sign-ins, passkeys and sessions in Bridge's local SQLite database.

Bridge has exactly one owner: the person whose Mac it runs on. Session tokens and
passwords are never stored — only their hashes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCRYPT = {"n": 2**15, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, maxmem=64 * 1024 * 1024, dklen=32, **SCRYPT
    )
    encoded = [base64.b64encode(value).decode() for value in (salt, digest)]
    return "scrypt${n}${r}${p}${salt}${digest}".format(**SCRYPT, salt=encoded[0], digest=encoded[1])


def verify_password(password: str, stored: str | None) -> bool:
    try:
        scheme, n, r, p, salt, digest = (stored or "").split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=64 * 1024 * 1024,
            dklen=32,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, base64.b64decode(digest))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Owner:
    email: str
    name: str
    has_password: bool
    created_at: float


@dataclass(frozen=True)
class Session:
    id: str  # Hash of the token; safe to show and to revoke by.
    kind: str  # "owner" or "setup" (first-run account creation only)
    created_at: float
    expires_at: float
    last_seen: float
    user_agent: str
    remember: bool


class AuthStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_owner (
                    id INTEGER PRIMARY KEY CHECK (id = 1), email TEXT NOT NULL, name TEXT NOT NULL,
                    password_hash TEXT, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_identities (
                    provider TEXT NOT NULL, subject TEXT NOT NULL, email TEXT, login TEXT,
                    created_at REAL NOT NULL, PRIMARY KEY (provider, subject));
                CREATE TABLE IF NOT EXISTS auth_passkeys (
                    credential_id TEXT PRIMARY KEY, public_key BLOB NOT NULL,
                    sign_count INTEGER NOT NULL, name TEXT NOT NULL, created_at REAL NOT NULL,
                    last_used REAL);
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at REAL NOT NULL,
                    expires_at REAL NOT NULL, last_seen REAL NOT NULL, user_agent TEXT,
                    remember INTEGER NOT NULL);
                """
            )
            # Passkeys belong to the address they were made on: localhost (this Mac) or the
            # phone address. Older ones are all localhost.
            columns = {row[1] for row in db.execute("PRAGMA table_info(auth_passkeys)")}
            if "rp_id" not in columns:
                db.execute(
                    "ALTER TABLE auth_passkeys ADD COLUMN rp_id TEXT NOT NULL DEFAULT 'localhost'"
                )

    def _db(self):
        return sqlite3.connect(self.path)

    # Owner --------------------------------------------------------------------------------

    def owner(self) -> Owner | None:
        with self._db() as db:
            row = db.execute(
                "SELECT email, name, password_hash, created_at FROM auth_owner WHERE id = 1"
            ).fetchone()
        return Owner(row[0], row[1], bool(row[2]), row[3]) if row else None

    def create_owner(self, email: str, name: str, password: str | None) -> Owner:
        with self._db() as db:
            try:
                db.execute(
                    "INSERT INTO auth_owner (id, email, name, password_hash, created_at) "
                    "VALUES (1, ?, ?, ?, ?)",
                    (email, name, hash_password(password) if password else None, time.time()),
                )
            except sqlite3.IntegrityError:
                raise ValueError("Bridge already has an owner account.") from None
        return self.owner()

    def update_profile(self, email: str, name: str) -> None:
        with self._db() as db:
            db.execute("UPDATE auth_owner SET email = ?, name = ? WHERE id = 1", (email, name))

    def set_password(self, password: str | None) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE auth_owner SET password_hash = ? WHERE id = 1",
                (hash_password(password) if password else None,),
            )

    def check_password(self, email: str, password: str) -> bool:
        with self._db() as db:
            row = db.execute("SELECT email, password_hash FROM auth_owner WHERE id = 1").fetchone()
        # Always hash, so wrong emails and wrong passwords take the same time.
        valid = verify_password(password, row[1] if row else None)
        return bool(row) and row[0].casefold() == email.casefold() and valid

    # Linked Google / GitHub sign-ins ------------------------------------------------------

    def identities(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT provider, subject, email, login, created_at FROM auth_identities"
            ).fetchall()
        keys = ("provider", "subject", "email", "login", "created_at")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def link_identity(self, provider: str, subject: str, email: str, login: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO auth_identities VALUES (?, ?, ?, ?, ?)",
                (provider, subject, email, login, time.time()),
            )

    def unlink_identity(self, provider: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM auth_identities WHERE provider = ?", (provider,))

    def has_identity(self, provider: str, subject: str) -> bool:
        with self._db() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM auth_identities WHERE provider = ? AND subject = ?",
                    (provider, subject),
                ).fetchone()
            )

    # Passkeys -----------------------------------------------------------------------------

    def passkeys(self, rp_id: str | None = "localhost") -> list[dict]:
        """Passkeys for one address (this Mac by default), or all of them with None."""
        query = (
            "SELECT credential_id, public_key, sign_count, name, created_at, last_used, rp_id "
            "FROM auth_passkeys"
        )
        with self._db() as db:
            if rp_id is None:
                rows = db.execute(query + " ORDER BY created_at").fetchall()
            else:
                rows = db.execute(
                    query + " WHERE rp_id = ? ORDER BY created_at", (rp_id,)
                ).fetchall()
        keys = (
            "credential_id",
            "public_key",
            "sign_count",
            "name",
            "created_at",
            "last_used",
            "rp_id",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def add_passkey(
        self,
        credential_id: str,
        public_key: bytes,
        sign_count: int,
        name: str,
        rp_id: str = "localhost",
    ):
        with self._db() as db:
            db.execute(
                "INSERT INTO auth_passkeys "
                "(credential_id, public_key, sign_count, name, created_at, last_used, rp_id) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (credential_id, public_key, sign_count, name, time.time(), rp_id),
            )

    def used_passkey(self, credential_id: str, sign_count: int) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE auth_passkeys SET sign_count = ?, last_used = ? WHERE credential_id = ?",
                (sign_count, time.time(), credential_id),
            )

    def remove_passkey(self, credential_id: str) -> bool:
        with self._db() as db:
            removed = db.execute(
                "DELETE FROM auth_passkeys WHERE credential_id = ?", (credential_id,)
            )
            return removed.rowcount > 0

    def sign_in_methods(self) -> int:
        owner = self.owner()
        return (
            int(bool(owner and owner.has_password)) + len(self.identities()) + len(self.passkeys())
        )

    # Sessions -----------------------------------------------------------------------------

    def create_session(
        self, kind: str, hours: float, user_agent: str = "", remember: bool = False
    ) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._db() as db:
            db.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))
            db.execute(
                "INSERT INTO auth_sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _token_hash(token),
                    kind,
                    now,
                    now + hours * 3600,
                    now,
                    user_agent[:200],
                    int(remember),
                ),
            )
        return token

    def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        now = time.time()
        key = _token_hash(token)
        with self._db() as db:
            row = db.execute(
                "SELECT token_hash, kind, created_at, expires_at, last_seen, user_agent, remember "
                "FROM auth_sessions WHERE token_hash = ? AND expires_at > ?",
                (key, now),
            ).fetchone()
            if row and now - row[4] > 60:
                db.execute(
                    "UPDATE auth_sessions SET last_seen = ? WHERE token_hash = ?", (now, key)
                )
        return Session(*row[:6], bool(row[6])) if row else None

    def sessions(self, kind: str = "owner") -> list[Session]:
        with self._db() as db:
            rows = db.execute(
                "SELECT token_hash, kind, created_at, expires_at, last_seen, user_agent, remember "
                "FROM auth_sessions WHERE kind = ? AND expires_at > ? ORDER BY last_seen DESC",
                (kind, time.time()),
            ).fetchall()
        return [Session(*row[:6], bool(row[6])) for row in rows]

    def end_session(self, token: str | None = None, session_id: str | None = None) -> None:
        key = session_id or (_token_hash(token) if token else None)
        if key:
            with self._db() as db:
                db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (key,))

    def end_other_sessions(self, keep_token: str) -> int:
        with self._db() as db:
            removed = db.execute(
                "DELETE FROM auth_sessions WHERE token_hash != ?", (_token_hash(keep_token),)
            )
            return removed.rowcount
