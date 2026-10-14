"""Accounts, the Macs signed in to them, and the credit ledger (SQLite, WAL).

Money is kept as integer micro-dollars of OpenAI cost (1_000_000 = $1). The balance is the
sum of the ledger; every entry has a unique reference, so a webhook delivered twice, or a
request charged twice, changes nothing the second time. Secrets (device tokens, license
keys) are stored only as SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path

MICRO = 1_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, created REAL NOT NULL, plan TEXT NOT NULL DEFAULT 'trial',
    status TEXT NOT NULL DEFAULT 'trial', renews_at TEXT, ends_at TEXT,
    customer_id TEXT, subscription_id TEXT, portal_url TEXT, email_hint TEXT
);
CREATE TABLE IF NOT EXISTS devices (
    token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL, name TEXT, created REAL NOT NULL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS licenses (
    key_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, at REAL NOT NULL,
    micros INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL UNIQUE, detail TEXT
);
CREATE INDEX IF NOT EXISTS ledger_account ON ledger (account_id, at);
CREATE TABLE IF NOT EXISTS voice (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, model TEXT NOT NULL, started REAL NOT NULL,
    ended REAL
);
CREATE TABLE IF NOT EXISTS trials (ip_hash TEXT NOT NULL, at REAL NOT NULL);
"""
ACCOUNT_KEYS = (
    "id",
    "created",
    "plan",
    "status",
    "renews_at",
    "ends_at",
    "customer_id",
    "subscription_id",
    "portal_url",
    "email_hint",
)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def micros(dollars: float) -> int:
    return round(dollars * MICRO)


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()
        self._db().executescript(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=10000")
            self._local.db = db
        return db

    # Accounts and devices -----------------------------------------------------------------

    def new_account(self, device_name: str = "") -> tuple[dict, str]:
        """A new account with this Mac signed in. Returns (account, device token)."""
        account_id = "acct_" + secrets.token_hex(10)
        token = "bdc_" + secrets.token_urlsafe(32)
        now = time.time()
        db = self._db()
        db.execute("BEGIN")
        try:
            db.execute("INSERT INTO accounts (id, created) VALUES (?, ?)", (account_id, now))
            db.execute(
                "INSERT INTO devices (token_hash, account_id, name, created, last_seen) "
                "VALUES (?, ?, ?, ?, ?)",
                (digest(token), account_id, device_name[:80], now, now),
            )
        except Exception:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return self.account(account_id), token

    def account(self, account_id: str) -> dict | None:
        row = (
            self._db()
            .execute(f"SELECT {', '.join(ACCOUNT_KEYS)} FROM accounts WHERE id = ?", (account_id,))
            .fetchone()
        )
        return dict(zip(ACCOUNT_KEYS, row, strict=True)) if row else None

    def account_for_token(self, token: str) -> dict | None:
        db = self._db()
        row = db.execute(
            "SELECT account_id, last_seen FROM devices WHERE token_hash = ?", (digest(token),)
        ).fetchone()
        if row is None:
            return None
        if time.time() - (row[1] or 0) > 300:
            db.execute(
                "UPDATE devices SET last_seen = ? WHERE token_hash = ?",
                (time.time(), digest(token)),
            )
        return self.account(row[0])

    def update_account(self, account_id: str, **fields) -> None:
        allowed = {k: v for k, v in fields.items() if k in ACCOUNT_KEYS and k != "id"}
        if not allowed:
            return
        sets = ", ".join(f"{k} = ?" for k in allowed)
        self._db().execute(
            f"UPDATE accounts SET {sets} WHERE id = ?", (*allowed.values(), account_id)
        )

    def account_for_subscription(self, subscription_id: str) -> dict | None:
        row = (
            self._db()
            .execute("SELECT id FROM accounts WHERE subscription_id = ?", (str(subscription_id),))
            .fetchone()
        )
        return self.account(row[0]) if row else None

    def move_device(self, token: str, account_id: str) -> None:
        self._db().execute(
            "UPDATE devices SET account_id = ? WHERE token_hash = ?", (account_id, digest(token))
        )

    def sign_out(self, token: str) -> None:
        self._db().execute("DELETE FROM devices WHERE token_hash = ?", (digest(token),))

    def devices(self, account_id: str) -> int:
        return (
            self._db()
            .execute("SELECT COUNT(*) FROM devices WHERE account_id = ?", (account_id,))
            .fetchone()[0]
        )

    # License keys (to sign in on another Mac) ----------------------------------------------

    def add_license(self, key: str, account_id: str) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO licenses (key_hash, account_id, created) VALUES (?, ?, ?)",
            (digest(key.strip()), account_id, time.time()),
        )

    def account_for_license(self, key: str) -> dict | None:
        row = (
            self._db()
            .execute("SELECT account_id FROM licenses WHERE key_hash = ?", (digest(key.strip()),))
            .fetchone()
        )
        return self.account(row[0]) if row else None

    # Trials ------------------------------------------------------------------------------

    def trials_from(self, ip: str, since: float) -> int:
        db = self._db()
        db.execute("DELETE FROM trials WHERE at < ?", (time.time() - 7 * 86400,))
        return db.execute(
            "SELECT COUNT(*) FROM trials WHERE ip_hash = ? AND at >= ?", (digest(ip), since)
        ).fetchone()[0]

    def note_trial(self, ip: str) -> None:
        self._db().execute("INSERT INTO trials VALUES (?, ?)", (digest(ip), time.time()))

    # Credit --------------------------------------------------------------------------------

    def add(self, account_id: str, amount: int, kind: str, ref: str, detail: str = "") -> bool:
        """Add (or, negative, take) credit once per reference. False if already done."""
        try:
            self._db().execute(
                "INSERT INTO ledger (account_id, at, micros, kind, ref, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (account_id, time.time(), int(amount), kind, ref, detail[:200]),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def balance(self, account_id: str) -> int:
        row = (
            self._db()
            .execute(
                "SELECT COALESCE(SUM(micros), 0) FROM ledger WHERE account_id = ?", (account_id,)
            )
            .fetchone()
        )
        return int(row[0])

    def spent_since(self, account_id: str, since: float) -> int:
        row = (
            self._db()
            .execute(
                "SELECT COALESCE(-SUM(micros), 0) FROM ledger WHERE account_id = ? AND at >= ? "
                "AND kind = 'usage'",
                (account_id, since),
            )
            .fetchone()
        )
        return int(row[0])

    def history(self, account_id: str, limit: int = 20) -> list[dict]:
        rows = (
            self._db()
            .execute(
                "SELECT at, micros, kind, detail FROM ledger "
                "WHERE account_id = ? AND kind != 'usage' "
                "ORDER BY at DESC LIMIT ?",
                (account_id, limit),
            )
            .fetchall()
        )
        return [{"at": r[0], "amount": r[1] / MICRO, "kind": r[2], "detail": r[3]} for r in rows]

    # Voice -------------------------------------------------------------------------------

    def start_voice(self, account_id: str, model: str) -> str:
        voice_id = "voice_" + secrets.token_hex(10)
        self._db().execute(
            "INSERT INTO voice (id, account_id, model, started) VALUES (?, ?, ?, ?)",
            (voice_id, account_id, model, time.time()),
        )
        return voice_id

    def end_voice(self, voice_id: str, account_id: str) -> dict | None:
        db = self._db()
        row = db.execute(
            "SELECT model, started, ended FROM voice WHERE id = ? AND account_id = ?",
            (voice_id, account_id),
        ).fetchone()
        if row is None or row[2] is not None:
            return None
        now = time.time()
        db.execute("UPDATE voice SET ended = ? WHERE id = ?", (now, voice_id))
        return {"id": voice_id, "model": row[0], "started": row[1], "ended": now}

    def open_voice(self, account_id: str, older_than: float) -> list[dict]:
        rows = (
            self._db()
            .execute(
                "SELECT id, model, started FROM voice WHERE account_id = ? AND ended IS NULL "
                "AND started < ?",
                (account_id, older_than),
            )
            .fetchall()
        )
        return [{"id": r[0], "model": r[1], "started": r[2]} for r in rows]

    def live_voice(self, account_id: str, since: float) -> int:
        return (
            self._db()
            .execute(
                "SELECT COUNT(*) FROM voice "
                "WHERE account_id = ? AND ended IS NULL AND started >= ?",
                (account_id, since),
            )
            .fetchone()[0]
        )
