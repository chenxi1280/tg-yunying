from __future__ import annotations

import base64
import hashlib
import hmac
from functools import lru_cache

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import get_settings

_VERSION_PREFIX = "enc:v"
_LEGACY_MARKER = "enc:v1:"
_V2_MARKER = "enc:v2:"


def _master_secret() -> bytes:
    return get_settings().session_secret_key.encode("utf-8")


@lru_cache(maxsize=1)
def _derive_fernet_key() -> bytes:
    """Derive a 32-byte Fernet key from the master secret via HKDF."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"tg-yunying-session-encrypt-v2",
        info=b"fernet-key",
    )
    raw = hkdf.derive(_master_secret())
    return base64.urlsafe_b64encode(raw)


def _derive_token_key() -> bytes:
    """Derive a separate key for token signing via HKDF."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"tg-yunying-token-signing",
        info=b"token-key",
    )
    return hkdf.derive(_master_secret())


def _derive_password_salt() -> bytes:
    """Derive a separate salt for password hashing via HKDF."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=16,
        salt=b"tg-yunying-password-salt",
        info=b"password-salt",
    )
    return hkdf.derive(_master_secret())


def get_password_salt() -> bytes:
    return _derive_password_salt()


def get_token_key() -> bytes:
    return _derive_token_key()


def encrypt_session(raw_session: str) -> str:
    key = _derive_fernet_key()
    fernet = Fernet(key)
    cipher = fernet.encrypt(raw_session.encode("utf-8"))
    return _V2_MARKER + base64.urlsafe_b64encode(cipher).decode("ascii")


def decrypt_session(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None

    # ---- legacy v1 path: homemade stream cipher ----
    if ciphertext.startswith(_LEGACY_MARKER):
        return _decrypt_legacy(ciphertext)

    # ---- v2 path: Fernet ----
    if ciphertext.startswith(_V2_MARKER):
        key = _derive_fernet_key()
        fernet = Fernet(key)
        payload = base64.urlsafe_b64decode(ciphertext.removeprefix(_V2_MARKER).encode("ascii"))
        return fernet.decrypt(payload).decode("utf-8")

    # Plaintext (no version prefix) — treat as unencrypted for backwards compat
    return ciphertext


def encrypt_secret(raw_value: str) -> str:
    return encrypt_session(raw_value)


def decrypt_secret(ciphertext: str | None) -> str | None:
    return decrypt_session(ciphertext)


# ---------------------------------------------------------------------------
# Legacy decryption: inlined here so new installations don't need the old
# keystream code except when decrypting previously-stored v1 ciphertexts.
# ---------------------------------------------------------------------------

def _decrypt_legacy(ciphertext: str) -> str | None:
    secret = _master_secret()
    payload = base64.urlsafe_b64decode(ciphertext.removeprefix(_LEGACY_MARKER).encode("ascii"))
    if len(payload) < 16:
        raise ValueError("legacy ciphertext too short")
    mac, cipher = payload[:16], payload[16:]
    expected = hmac.new(secret, cipher, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(mac, expected):
        raise ValueError("session ciphertext integrity check failed")
    plaintext = bytes(byte ^ key for byte, key in zip(cipher, _legacy_keystream(secret, len(cipher)), strict=True))
    return plaintext.decode("utf-8")


def _legacy_keystream(secret: bytes, length: int) -> bytes:
    chunks: list[bytes] = []
    counter = 0
    while sum(len(chunk) for chunk in chunks) < length:
        chunks.append(hashlib.sha256(secret + counter.to_bytes(4, "big")).digest())
        counter += 1
    return b"".join(chunks)[:length]
