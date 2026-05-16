from __future__ import annotations

from datetime import datetime
from typing import Any


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_int_list(value: Any) -> list[int]:
    if not value:
        return []
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    items: list[int] = []
    for item in raw_items:
        parsed = as_int(item)
        if parsed is not None and parsed not in items:
            items.append(parsed)
    return items


def iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()
