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
    window = deboost_pacing_window(task, action.scheduled_at or current)
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
        expires_at=_reservation_expiry(current, action.scheduled_at),
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


def consume_reserved_reservation(session: Session, action_id: str) -> SearchRankDeboostClickReservation | None:
    reservation = reservation_for_action(session, action_id)
    if reservation is None or reservation.status != "reserved":
        return reservation
    return consume_reservation(session, action_id)


def release_reservation(session: Session, action_id: str) -> SearchRankDeboostClickReservation | None:
    reservation = _require_reservation(session, action_id)
    _require_status(reservation, {"reserved"})
    reservation.status = "released"
    reservation.consumed_count = 0
    session.flush()
    return reservation


def release_reserved_reservation(session: Session, action_id: str) -> SearchRankDeboostClickReservation | None:
    reservation = reservation_for_action(session, action_id)
    if reservation is None or reservation.status != "reserved":
        return reservation
    return release_reservation(session, action_id)


def release_expired_pending_reservations(
    session: Session,
    *,
    tenant_id: int,
    now_value: datetime | None = None,
) -> int:
    """释放尚未进入 Gateway 调用边界的过期预留。"""
    current = now_value or _now()
    rows = session.execute(
        select(SearchRankDeboostClickReservation, Action)
        .join(Action, Action.id == SearchRankDeboostClickReservation.action_id)
        .where(
            SearchRankDeboostClickReservation.tenant_id == tenant_id,
            SearchRankDeboostClickReservation.status == "reserved",
            SearchRankDeboostClickReservation.expires_at <= current,
            Action.status == "pending",
        )
        .with_for_update()
    ).all()
    for reservation, action in rows:
        reservation.status = "released"
        reservation.consumed_count = 0
        action.status = "skipped"
        action.executed_at = current
        action.result = {
            **(action.result or {}),
            "success": False,
            "error_code": "rank_deboost_reservation_expired",
            "error_message": "等待执行超过预留有效期，已释放配额并等待重新规划",
            "auto_check": "跳过",
            "validation_stage": "search_rank_deboost_pacing",
        }
    session.flush()
    return len(rows)


def reopen_released_reservation(
    session: Session,
    action_id: str,
    *,
    now_value: datetime | None = None,
) -> SearchRankDeboostClickReservation:
    reservation = _require_reservation(session, action_id)
    _require_status(reservation, {"released"})
    current = now_value or _now()
    reservation.status = "reserved"
    reservation.consumed_count = 0
    reservation.expires_at = current + timedelta(minutes=RESERVATION_TTL_MINUTES)
    session.flush()
    return reservation


def mark_reservation_unknown(session: Session, action_id: str) -> SearchRankDeboostClickReservation | None:
    reservation = _require_reservation(session, action_id)
    _require_status(reservation, {"reserved"})
    reservation.status = "unknown"
    reservation.consumed_count = reservation.reserved_count
    session.flush()
    return reservation


def mark_reserved_reservation_unknown(session: Session, action_id: str) -> SearchRankDeboostClickReservation | None:
    reservation = reservation_for_action(session, action_id)
    if reservation is None or reservation.status != "reserved":
        return reservation
    return mark_reservation_unknown(session, action_id)


def gateway_reservation_blocker(
    session: Session,
    action_id: str,
    *,
    now_value: datetime | None = None,
) -> str:
    """返回 Gateway 前必须阻断的 reservation 状态，并在过期时释放额度。"""
    reservation = reservation_for_action(session, action_id)
    if reservation is None:
        return "rank_deboost_reservation_missing"
    if reservation.status != "reserved":
        return f"rank_deboost_reservation_{reservation.status}"
    if reservation.expires_at > (now_value or _now()):
        return ""
    release_reservation(session, action_id)
    return "rank_deboost_reservation_expired"


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


def _reservation_expiry(current: datetime, scheduled_at: datetime | None) -> datetime:
    planned = scheduled_at or current
    if planned.tzinfo is not None:
        planned = planned.replace(tzinfo=None)
    return max(current, planned) + timedelta(minutes=RESERVATION_TTL_MINUTES)


__all__ = [
    "ACTIVE_RESERVATION_STATUSES",
    "consume_reservation",
    "consume_reserved_reservation",
    "gateway_reservation_blocker",
    "mark_reservation_unknown",
    "mark_reserved_reservation_unknown",
    "reopen_released_reservation",
    "release_reserved_reservation",
    "release_expired_pending_reservations",
    "release_reservation",
    "reservation_for_action",
    "reserve_click",
]
