"""Timezone-safe datetime helpers for hard-hourly and claim paths.

Prefer comparing in a shared zone rather than stripping tzinfo.
"""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.timezone import BEIJING_TZ


UTC = timezone.utc


def ensure_aware(value: datetime, *, default_zone: tzinfo | None = None) -> datetime:
    """Return an aware datetime. Naive values are treated as default_zone wall time."""
    zone = default_zone or BEIJING_TZ
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value


def to_zone(value: datetime, zone: tzinfo | None = None) -> datetime:
    target = zone or BEIJING_TZ
    return ensure_aware(value, default_zone=target).astimezone(target)


def compare_datetimes(left: datetime, right: datetime, *, default_zone: ZoneInfo | None = None) -> int:
    """Return -1/0/1 for left < / == / > right using a common zone."""
    zone = default_zone or BEIJING_TZ
    left_z = to_zone(left, zone)
    right_z = to_zone(right, zone)
    if left_z < right_z:
        return -1
    if left_z > right_z:
        return 1
    return 0


def is_before(left: datetime, right: datetime, *, default_zone: ZoneInfo | None = None) -> bool:
    return compare_datetimes(left, right, default_zone=default_zone) < 0


def is_after_or_equal(left: datetime, right: datetime, *, default_zone: ZoneInfo | None = None) -> bool:
    return compare_datetimes(left, right, default_zone=default_zone) >= 0


def parse_zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or "Asia/Shanghai"))
    except ZoneInfoNotFoundError:
        return BEIJING_TZ


__all__ = [
    "UTC",
    "compare_datetimes",
    "ensure_aware",
    "is_after_or_equal",
    "is_before",
    "parse_zone",
    "to_zone",
]
