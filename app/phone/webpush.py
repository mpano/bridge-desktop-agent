"""Web Push for the phone app: VAPID (RFC 8292) and aes128gcm encryption (RFC 8291).

The push service (Apple's, for an iPhone) carries an encrypted message it can't read; only
the phone that subscribed can decrypt it.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Only real browser push services, so a subscription can't make Bridge call anything else.
PUSH_HOSTS = (
    "web.push.apple.com",
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "notify.windows.com",
)
RECORD_SIZE = 4096


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def allowed_endpoint(endpoint: str) -> bool:
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and len(endpoint) <= 1000
        and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)
    )


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(
    plaintext: bytes,
    p256dh: str,
    auth: str,
    *,
    sender: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """One aes128gcm record for the subscription's keys (RFC 8291 §3–4)."""
    receiver_bytes = unb64(p256dh)
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver_bytes)
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    sender_bytes = _public_bytes(sender.public_key())
    salt = salt or os.urandom(16)
    shared = sender.exchange(ec.ECDH(), receiver)
    ikm = _hkdf(unb64(auth), shared, b"WebPush: info\x00" + receiver_bytes + sender_bytes, 32)
    key = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    record = AESGCM(key).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack("!IB", RECORD_SIZE, len(sender_bytes)) + sender_bytes
    return header + record


class Vapid:
    """The key that proves pushes come from this Bridge."""

    def __init__(self, pem: bytes | None = None):
        self.key = (
            serialization.load_pem_private_key(pem, password=None)
            if pem
            else ec.generate_private_key(ec.SECP256R1())
        )

    def pem(self) -> bytes:
        return self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    @property
    def public_key(self) -> str:
        return b64(_public_bytes(self.key.public_key()))

    def header(self, endpoint: str, subject: str, now: float | None = None) -> str:
        parts = urlsplit(endpoint)
        claims = {
            "aud": f"{parts.scheme}://{parts.netloc}",
            "exp": int((now or time.time()) + 12 * 3600),
            "sub": subject,
        }
        signing = (
            b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
            + "."
            + b64(json.dumps(claims, separators=(",", ":")).encode())
        )
        r, s = decode_dss_signature(self.key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
        token = signing + "." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={token}, k={self.public_key}"
