from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, Task, TgAccount
from app.models.search_rank_deboost import SearchRankDeboostClickReservation
from app.services._common import _now
from app.services.task_center.search_rank_deboost_pacing import deboost_pacing_window


RESERVATION_TTL_MINUTES = 30
ACTIVE_RESERVATION_STATUSES = {"reserved", "consumed", "unknown"}


def reserve_click(
    session: Session,
    *,
    task: Task,
    action: Action,
    account: TgAccount,
    account_pool_id: int,
    keyword_hash: str,
    now_value: datetime | None = None,
) -> SearchRankDeboostClickReservation:
    existing = reservation_for_action(session, action.id)
    if existing is not None:
        return existing
    current = now_value or _now()
    window = deboost_pacing_window(task, current)
    reservation = SearchRankDeboostClickReservation(
        tenant_id=task.tenant_id,
        task_id=task.id,
        action_id=action.id,
        account_id=account.id,
        account_pool_id=account_pool_id,
        keyword_hash=keyword_hash,
        local_date=window.local_date,
        hour_bucket=window.hour_start,
        reserved_count=1,
        consumed_count=0,
        status="reserved",
        expires_at=current + timedelta(minutes=RESERVATION_TTL_MINUTES),
    )
    session.add(reservation)
    session.flush()
    return reservation


def consume_reservation(session: Session, action_id: str) -> SearchRankDeboostClickReservation | None:
    reservation = _require_reservation(session, action_id)
    _require_status(reservation, {"reserved"})
    reservation.status = "consumed"
    reservation.consumed_count = reservation.reserved_count
    session.flush()
    return reservation


def release_reservation(session: Session, action_id: str) -> SearchRankDeboostClickReservation | None:
    reservation = _require_reservation(session, action_id)
    _require_status(reservation, {"reserved"})
    reservation.status = "released"
    reservation.consumed_count = 0
    session.flush()
    return reservation


def mark_reservation_unknown(session: Session, action_id: str) -> SearchRankDeboostClickReservation | None:
    reservation = _require_reservation(session, action_id)
    _require_status(reservation, {"reserved"})
    reservation.status = "unknown"
    reservation.consumed_count = reservation.reserved_count
    session.flush()
    return reservation


def reservation_for_action(session: Session, action_id: str) -> SearchRankDeboostClickReservation | None:
    return session.scalar(
        select(SearchRankDeboostClickReservation).where(
            SearchRankDeboostClickReservation.action_id == action_id,
        ).limit(1)
    )


def _require_reservation(session: Session, action_id: str) -> SearchRankDeboostClickReservation:
    reservation = reservation_for_action(session, action_id)
    if reservation is None:
        raise ValueError("rank_deboost_reservation_missing")
    return reservation


def _require_status(reservation: SearchRankDeboostClickReservation, allowed: set[str]) -> None:
    if reservation.status not in allowed:
        raise ValueError(f"rank_deboost_reservation_invalid_state:{reservation.status}")


__all__ = [
    "ACTIVE_RESERVATION_STATUSES",
    "consume_reservation",
    "mark_reservation_unknown",
    "release_reservation",
    "reservation_for_action",
    "reserve_click",
]
