"""Intersect existing behavior windows with an account/task timeline."""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ReservationTiming:
    release_at: datetime
    effective_at: datetime
    failure_code: str = ""


def effective_claim_at(due_at: datetime, account_not_before: datetime | None) -> datetime:
    return due_at if account_not_before is None or due_at >= account_not_before else account_not_before


def resolve_windowed_timing(
    *,
    desired_at: datetime,
    deadline_at: datetime | None,
    window_not_before: Callable[[datetime], datetime | None],
    timeline_not_before: Callable[[datetime], datetime | None],
) -> ReservationTiming:
    candidate = desired_at
    while True:
        release_at = window_not_before(candidate)
        if release_at is None:
            return ReservationTiming(candidate, candidate, "account_behavior_session_unavailable")
        effective_at = effective_claim_at(release_at, timeline_not_before(release_at))
        if deadline_at is not None and effective_at >= deadline_at:
            return ReservationTiming(release_at, effective_at, "account_timeline_conflict")
        window_at = window_not_before(effective_at)
        if window_at is None:
            return ReservationTiming(release_at, effective_at, "account_behavior_session_unavailable")
        if window_at == effective_at:
            return ReservationTiming(release_at, effective_at)
        # Each repeat advances to a later existing window, then checks its timeline anew.
        candidate = window_at
