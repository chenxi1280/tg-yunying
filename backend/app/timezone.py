from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BEIJING_TIMEZONE_NAME = "Asia/Shanghai"
BEIJING_TZ = ZoneInfo(BEIJING_TIMEZONE_NAME)


def beijing_now() -> datetime:
    """Return a UTC-naive datetime whose wall-clock value is Beijing time."""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def as_beijing(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)


def as_beijing_aware(value: datetime) -> datetime:
    normalized = as_beijing(value)
    return normalized.replace(tzinfo=BEIJING_TZ)


def beijing_day_bounds(value: datetime | None = None) -> tuple[datetime, datetime]:
    current = as_beijing(value) or beijing_now()
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)
