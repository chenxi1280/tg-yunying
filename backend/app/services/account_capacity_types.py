from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.models import SchedulingSetting


@dataclass(frozen=True)
class AccountCapacityDecision:
    available: bool
    defer_until: datetime | None = None
    reason_code: str = ""
    reason: str = ""


@dataclass(frozen=True)
class AccountCapacityReservation:
    account_id: int
    scheduled_at: datetime


@dataclass
class AccountCapacityCache:
    occupied_counts: dict[tuple, int] = field(default_factory=dict)
    last_occupied_at: dict[tuple, datetime | None] = field(default_factory=dict)
    occupied_timelines: dict[tuple, tuple[datetime, ...]] = field(default_factory=dict)
    settings: dict[int, SchedulingSetting] = field(default_factory=dict)
    primed_windows: set[tuple] = field(default_factory=set)


__all__ = ["AccountCapacityCache", "AccountCapacityDecision", "AccountCapacityReservation"]
