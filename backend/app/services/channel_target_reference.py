from __future__ import annotations

from app.models import OperationTarget


TELEGRAM_REFERENCE_PREFIXES = (
    "https://t.me/",
    "http://t.me/",
    "t.me/",
    "https://telegram.me/",
    "http://telegram.me/",
    "telegram.me/",
)
TELEGRAM_INVITE_MARKERS = ("+", "/+", "/joinchat/")


def channel_read_reference(channel: OperationTarget) -> str:
    username = str(channel.username or "").strip()
    if not username:
        return str(channel.tg_peer_id or "").strip()
    if username.startswith("+") or any(marker in username for marker in TELEGRAM_INVITE_MARKERS[1:]):
        return str(channel.tg_peer_id or "").strip()
    if username.startswith(TELEGRAM_REFERENCE_PREFIXES):
        return username
    return f"@{username.lstrip('@')}"


__all__ = ["channel_read_reference"]
