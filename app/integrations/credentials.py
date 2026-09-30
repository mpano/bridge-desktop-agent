"""Explicit macOS Keychain backend; never fall back to plaintext storage."""

import json
import sys
from typing import Protocol

from app.integrations.models import Account, IntegrationError


class CredentialStore(Protocol):
    def load(self) -> dict[str, Account]: ...
    def save(self, accounts: dict[str, Account]) -> None: ...


class KeychainCredentials:
    service = "Bridge Connected Services"
    username = "accounts-v1"

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
            return {
                key: Account.model_validate(value) for key, value in json.loads(raw or "{}").items()
            }
        except Exception:
            raise IntegrationError(
                "Could not read Bridge accounts from macOS Keychain. Check access; no plaintext "
                "fallback is used."
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
            raise IntegrationError("Could not save Bridge accounts in macOS Keychain.") from None
