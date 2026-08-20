from __future__ import annotations

from datetime import datetime
import hashlib
import json


def authorization_fingerprint_digest(item) -> str:
    payload = (
        int(getattr(item, "api_id", 0) or 0),
        _text(getattr(item, "app_name", "")),
        _text(getattr(item, "device_model", "")),
        _text(getattr(item, "platform", "")),
        _text(getattr(item, "system_version", "")),
        _text(getattr(item, "app_version", "")),
        _timestamp(getattr(item, "date_created", None)),
    )
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _text(value: str | None) -> str:
    return str(value or "").strip()


def _timestamp(value: datetime | None) -> str:
    return value.replace(microsecond=0).isoformat() if value else ""


__all__ = ["authorization_fingerprint_digest"]
