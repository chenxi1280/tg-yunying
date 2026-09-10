"""Identity is the AuthKey, independent of App, metadata and serialized DC address."""
from __future__ import annotations

import hashlib


def authorization_identity(raw_session: str) -> str:
    from telethon.sessions import StringSession

    session = StringSession(raw_session)
    auth_key = session.auth_key
    if auth_key is None or not auth_key.key:
        raise ValueError("telegram_authorization_key_missing")
    return hashlib.sha256(bytes(auth_key.key)).hexdigest()
