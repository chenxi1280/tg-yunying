"""Canonical hashing shared by account inventory and execution evidence."""
from datetime import datetime, timezone
import hashlib
import json
from typing import Mapping


def canonical_state_hash(payload: object) -> str:
    encoded = json.dumps(
        _canonical_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc_text(value: datetime | None) -> str:
    if value is None:
        return ""
    observed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc).isoformat(
        timespec="microseconds",
    ).replace("+00:00", "Z")


def _canonical_value(value: object) -> object:
    if isinstance(value, datetime):
        return _utc_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, set):
        normalized = [_canonical_value(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
