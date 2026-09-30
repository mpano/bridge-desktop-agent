"""Passkeys (Touch ID / iCloud Keychain) for the Bridge owner, via py_webauthn.

WebAuthn needs a domain name, so passkeys work on http://localhost:<port>, not on a
bare IP address such as 127.0.0.1.
"""

from __future__ import annotations

import json
import time

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

RP_ID = "localhost"
USER_ID = b"bridge-owner"  # One owner per Bridge installation.
CHALLENGE_SECONDS = 300


class PasskeyError(RuntimeError):
    """Safe to show to the user."""


class Passkeys:
    def __init__(self, store):
        self.store = store
        self._challenges: dict[str, tuple[bytes, float]] = {}

    def _remember(self, purpose: str, challenge: bytes) -> None:
        self._challenges[purpose] = (challenge, time.monotonic() + CHALLENGE_SECONDS)

    def _take(self, purpose: str) -> bytes:
        challenge, expires = self._challenges.pop(purpose, (None, 0))
        if challenge is None or expires <= time.monotonic():
            raise PasskeyError("That request expired. Try again.")
        return challenge

    @staticmethod
    def check_origin(origin: str) -> None:
        if not origin.startswith("http://localhost:"):
            raise PasskeyError(
                "Passkeys work at http://localhost:8000. Open the dashboard from the Bridge menu."
            )

    def registration_options(self, owner) -> dict:
        options = generate_registration_options(
            rp_id=RP_ID,
            rp_name="Bridge",
            user_id=USER_ID,
            user_name=owner.email,
            user_display_name=owner.name,
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(item["credential_id"]))
                for item in self.store.passkeys()
            ],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        self._remember("register", options.challenge)
        return json.loads(options_to_json(options))

    def register(self, credential: dict, origin: str, name: str) -> dict:
        self.check_origin(origin)
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=self._take("register"),
                expected_origin=origin,
                expected_rp_id=RP_ID,
                require_user_verification=True,
            )
        except PasskeyError:
            raise
        except Exception:
            raise PasskeyError("The passkey couldn't be verified. Try again.") from None
        credential_id = bytes_to_base64url(verified.credential_id)
        self.store.add_passkey(
            credential_id, verified.credential_public_key, verified.sign_count, name
        )
        return {"credential_id": credential_id, "name": name}

    def authentication_options(self) -> dict:
        options = generate_authentication_options(
            rp_id=RP_ID, user_verification=UserVerificationRequirement.REQUIRED
        )
        self._remember("login", options.challenge)
        return json.loads(options_to_json(options))

    def authenticate(self, credential: dict, origin: str) -> None:
        self.check_origin(origin)
        known = {item["credential_id"]: item for item in self.store.passkeys()}
        stored = known.get(str(credential.get("id", "")))
        if stored is None:
            self._challenges.pop("login", None)
            raise PasskeyError("That passkey isn't registered with this Bridge.")
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=self._take("login"),
                expected_rp_id=RP_ID,
                expected_origin=origin,
                credential_public_key=stored["public_key"],
                credential_current_sign_count=stored["sign_count"],
                require_user_verification=True,
            )
        except PasskeyError:
            raise
        except Exception:
            raise PasskeyError("The passkey couldn't be verified. Try again.") from None
        self.store.used_passkey(stored["credential_id"], verified.new_sign_count)
