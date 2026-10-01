"""Explicit macOS Keychain backend; never fall back to plaintext storage."""

import json
import sys
from typing import Protocol

from app.integrations.models import Account, IntegrationError


class CredentialStore(Protocol):
    def load(self) -> dict[str, Account]: ...
    def save(self, accounts: dict[str, Account]) -> None: ...


class KeychainCredentials:
    """Accounts live in one Keychain entry that Bridge.app itself owns.

    Keychain entries can only be changed by the app that created them. Bridge used to run as
    Python, so the original entry ("accounts-v1") belongs to Python; Bridge reads it once to
    migrate and from then on reads and writes its own entry ("accounts-v2").
    """

    service = "Bridge Connected Services"
    username = "accounts-v2"
    legacy_usernames = ("accounts-v1",)

    def _backend(self):
        if sys.platform != "darwin":
            raise IntegrationError("Connected accounts require macOS Keychain.")
        try:
            from keyring.backends.macOS import Keyring

            return Keyring()
        except ImportError:
            raise IntegrationError(
                "Install connected services: pip install -e '.[integrations]'"
            ) from None

    def load(self) -> dict[str, Account]:
        backend = self._backend()
        try:
            raw = backend.get_password(self.service, self.username)
            for legacy in self.legacy_usernames:
                if raw is not None:
                    break
                raw = backend.get_password(self.service, legacy)
            return {
                key: Account.model_validate(value) for key, value in json.loads(raw or "{}").items()
            }
        except Exception:
            raise IntegrationError(
                "Could not read Bridge accounts from macOS Keychain. If macOS asked to allow "
                "access, choose Always Allow. No plaintext fallback is used."
            ) from None

    def save(self, accounts: dict[str, Account]) -> None:
        backend = self._backend()
        try:
            backend.set_password(
                self.service,
                self.username,
                json.dumps({key: value.credential_json() for key, value in accounts.items()}),
            )
        except Exception:
            raise IntegrationError(
                "Could not save Bridge accounts in macOS Keychain. If macOS asked to allow "
                "access, choose Always Allow and try again."
            ) from None
