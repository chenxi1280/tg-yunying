from __future__ import annotations

from datetime import datetime, timedelta
import hashlib


LATE_RECOVERY_MIN_SECONDS = 60
LATE_RECOVERY_BASE_GAP_RATIO = 0.8
LATE_RECOVERY_JITTER_GAP_RATIO = 1.0


def late_admission_not_before(
    *,
    action_id: str,
    release_at: datetime,
    now_at: datetime,
    gap_seconds: int,
    deadline_at: datetime,
) -> datetime:
    """Freeze a paced recovery point for a materially overdue bound action."""
    lateness = (now_at - release_at).total_seconds()
    threshold = max(LATE_RECOVERY_MIN_SECONDS, gap_seconds * 0.25)
    if lateness <= threshold:
        return release_at
    ratio = _stable_ratio(action_id)
    delay = gap_seconds * (
        LATE_RECOVERY_BASE_GAP_RATIO + ratio * LATE_RECOVERY_JITTER_GAP_RATIO
    )
    return min(now_at + timedelta(seconds=delay), deadline_at)


def _stable_ratio(action_id: str) -> float:
    digest = hashlib.sha256(action_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") / ((1 << 32) - 1)


__all__ = ["late_admission_not_before"]
