from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, union_all
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AccountPacingReservation, Action, FulfillmentRemoteFact, TgAccount
from app.services._common import _now
from app.timezone import BEIJING_TZ

from .source_pacing import latest_wall_datetime, wall_datetime


ACCOUNT_SOFT_PACING_POLICY_VERSION = "account_soft_pacing_v1"
TIMELINE_PAGE_SIZE = 128
_OPEN_GUARD_STATUSES = ("pending", "claiming", "executing", "retryable_failed", "unknown_after_send")
_OPEN_RESERVATION_STATES = ("reserved", "bound")


class AccountPacingDeadlineExceeded(RuntimeError):
    pass


class AccountPacingLockUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PacingClaimDecision:
    allowed: bool
    effective_claim_at: datetime | None = None
    reason_code: str = ""


def _wall(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)


def lock_account_pacing(session: Session, account_id: int) -> None:
    statement = select(TgAccount.id).where(TgAccount.id == account_id)
    if session.get_bind().dialect.name == "sqlite":
        if session.scalar(statement) is None:
            raise ValueError("account_pacing_account_not_found")
        return
    if session.scalar(statement.with_for_update(skip_locked=True)) is not None:
        return
    if session.scalar(statement) is None:
        raise ValueError("account_pacing_account_not_found")
    raise AccountPacingLockUnavailable("account_pacing_lock_busy")


def account_policy_not_before(
    session: Session,
    account_id: int,
    *,
    tenant_id: int,
    now_value: datetime | None = None,
    deadline_at: datetime | None = None,
    exclude_action_id: str | None = None,
    exclude_slot_key: str | None = None,
) -> datetime | None:
    desired_at = _wall(now_value or _now())
    if desired_at is None:
        return None
    gap = timedelta(seconds=max(1, int(get_settings().account_soft_pacing_min_gap_seconds)))
    points = _account_timeline_points(
        session,
        tenant_id,
        account_id,
        desired_at=desired_at,
        gap=gap,
        deadline_at=deadline_at,
        exclude_action_id=exclude_action_id,
        exclude_slot_key=exclude_slot_key,
    )
    return _earliest_available_time(desired_at, points, gap)


def reserve_account_pacing(
    session: Session,
    *,
    tenant_id: int,
    task_id: str,
    account_id: int,
    slot_key: str,
    due_at: datetime,
    deadline_at: datetime | None,
    release_not_before_at: datetime | None = None,
) -> AccountPacingReservation:
    due_at = wall_datetime(due_at)
    deadline_at = wall_datetime(deadline_at) if deadline_at is not None else None
    release_not_before_at = (
        wall_datetime(release_not_before_at)
        if release_not_before_at is not None
        else due_at
    )
    lock_account_pacing(session, account_id)
    existing = _reservation_for_any_slot(session, tenant_id, account_id, slot_key)
    if existing is not None:
        if existing.state in _OPEN_RESERVATION_STATES:
            return existing
        if existing.state == "missed":
            raise AccountPacingDeadlineExceeded("pacing_slot_already_missed")
        raise ValueError("account_pacing_reservation_state_invalid")
    release_at = latest_wall_datetime(due_at, release_not_before_at)
    not_before = account_policy_not_before(
        session,
        account_id,
        tenant_id=tenant_id,
        now_value=release_at,
        deadline_at=deadline_at,
    )
    effective_at = effective_claim_at(release_at, not_before)
    if deadline_at is not None and not _before_deadline(effective_at, deadline_at):
        raise AccountPacingDeadlineExceeded("account_timeline_conflict")
    reservation = AccountPacingReservation(
        tenant_id=tenant_id,
        task_id=task_id,
        account_id=account_id,
        pacing_slot_key=slot_key,
        policy_version=ACCOUNT_SOFT_PACING_POLICY_VERSION,
        due_at=due_at,
        release_not_before_at=release_at,
        effective_claim_at=effective_at,
        source_deadline_at=deadline_at,
    )
    session.add(reservation)
    session.flush()
    return reservation


def bind_account_pacing_reservation(
    reservation: AccountPacingReservation,
    action: Action,
) -> None:
    reservation.action_id = action.id
    reservation.state = "bound"
    reservation.version += 1
    action.scheduled_at = reservation.effective_claim_at
    action.release_not_before_at = reservation.release_not_before_at
    action.effective_claim_at = reservation.effective_claim_at


def bind_account_pacing_reservation_for_slot(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    slot_key: str,
    action: Action,
) -> None:
    reservation = _reservation_for_slot(session, tenant_id, account_id, slot_key)
    if reservation is None:
        raise ValueError("account_pacing_reservation_missing")
    bind_account_pacing_reservation(reservation, action)


def effective_claim_at(due_at: datetime, account_not_before: datetime | None) -> datetime:
    return due_at if account_not_before is None or due_at >= account_not_before else account_not_before


def revalidate_action_pacing_before_claim(
    session: Session,
    action: Action,
    *,
    now_value: datetime,
) -> PacingClaimDecision:
    if not action.pacing_slot_key or not action.account_id:
        return PacingClaimDecision(True, action.scheduled_at)
    try:
        lock_account_pacing(session, int(action.account_id))
    except AccountPacingLockUnavailable:
        return PacingClaimDecision(
            False,
            action.scheduled_at,
            "account_pacing_lock_busy",
        )
    desired_at = max(
        value
        for value in (
            _wall(now_value),
            _wall(action.pacing_due_at),
            _wall(action.release_not_before_at),
        )
        if value is not None
    )
    reservation = _reservation_for_slot(
        session,
        action.tenant_id,
        int(action.account_id),
        str(action.pacing_slot_key),
    )
    if reservation is None:
        raise ValueError("account_pacing_reservation_missing")
    not_before = account_policy_not_before(
        session,
        int(action.account_id),
        tenant_id=action.tenant_id,
        now_value=desired_at,
        deadline_at=reservation.source_deadline_at,
        exclude_action_id=action.id,
        exclude_slot_key=str(action.pacing_slot_key),
    )
    effective_at = effective_claim_at(desired_at, not_before)
    if reservation.source_deadline_at and not _before_deadline(
        effective_at, reservation.source_deadline_at,
    ):
        return PacingClaimDecision(False, effective_at, "pacing_claim_deadline_exceeded")
    if effective_at <= _wall(now_value):
        _sync_claim_time(action, reservation, effective_at)
        return PacingClaimDecision(True, effective_at)
    _defer_action_claim(action, reservation, effective_at)
    return PacingClaimDecision(False, effective_at, "account_timeline_conflict")


def _defer_action_claim(
    action: Action,
    reservation: AccountPacingReservation,
    effective_at: datetime,
) -> None:
    action.scheduled_at = effective_at
    action.effective_claim_at = effective_at
    action.action_version = int(action.action_version or 1) + 1
    action.result = {
        **(action.result or {}),
        "claim_pacing_deferred": {
            "reason_code": "account_timeline_conflict",
            "effective_claim_at": effective_at.isoformat(),
        },
    }
    reservation.effective_claim_at = effective_at
    reservation.version = int(reservation.version or 1) + 1


def _sync_claim_time(
    action: Action,
    reservation: AccountPacingReservation,
    effective_at: datetime,
) -> None:
    action.effective_claim_at = effective_at
    if reservation.effective_claim_at == effective_at:
        return
    reservation.effective_claim_at = effective_at
    reservation.version = int(reservation.version or 1) + 1


def _reservation_for_slot(
    session: Session,
    tenant_id: int,
    account_id: int,
    slot_key: str,
) -> AccountPacingReservation | None:
    return session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.tenant_id == tenant_id,
        AccountPacingReservation.account_id == account_id,
        AccountPacingReservation.pacing_slot_key == slot_key,
        AccountPacingReservation.state.in_(_OPEN_RESERVATION_STATES),
    ))


def _reservation_for_any_slot(
    session: Session,
    tenant_id: int,
    account_id: int,
    slot_key: str,
) -> AccountPacingReservation | None:
    return session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.tenant_id == tenant_id,
        AccountPacingReservation.account_id == account_id,
        AccountPacingReservation.pacing_slot_key == slot_key,
    ))


def _timeline_union(
    *,
    tenant_id: int,
    account_id: int,
    start_at: datetime,
    end_at: datetime | None,
    exclude_action_id: str | None,
    exclude_slot_key: str | None,
):
    action_filters = [
        Action.tenant_id == tenant_id,
        Action.account_id == account_id,
        Action.status.in_(_OPEN_GUARD_STATUSES),
        Action.scheduled_at.is_not(None),
        Action.scheduled_at >= start_at,
    ]
    fact_filters = [
        FulfillmentRemoteFact.tenant_id == tenant_id,
        Action.account_id == account_id,
        FulfillmentRemoteFact.fact_kind.in_((
            "remote_message_observed",
            "view_observed",
            "reaction_observed",
        )),
        FulfillmentRemoteFact.observed_at >= start_at,
    ]
    reservation_filters = [
        AccountPacingReservation.tenant_id == tenant_id,
        AccountPacingReservation.account_id == account_id,
        AccountPacingReservation.state.in_(_OPEN_RESERVATION_STATES),
        AccountPacingReservation.effective_claim_at >= start_at,
    ]
    if exclude_action_id:
        action_filters.append(Action.id != exclude_action_id)
        fact_filters.append(FulfillmentRemoteFact.action_id != exclude_action_id)
    if exclude_slot_key:
        reservation_filters.append(
            AccountPacingReservation.pacing_slot_key != exclude_slot_key,
        )
    if end_at is not None:
        action_filters.append(Action.scheduled_at < end_at)
        fact_filters.append(FulfillmentRemoteFact.observed_at < end_at)
        reservation_filters.append(AccountPacingReservation.effective_claim_at < end_at)
    return union_all(
        select(Action.scheduled_at.label("timeline_at")).where(*action_filters),
        select(FulfillmentRemoteFact.observed_at.label("timeline_at"))
        .join(Action, Action.id == FulfillmentRemoteFact.action_id)
        .where(*fact_filters),
        select(AccountPacingReservation.effective_claim_at.label("timeline_at")).where(*reservation_filters),
    ).subquery()


def _account_timeline_points(
    session: Session,
    tenant_id: int,
    account_id: int,
    *,
    desired_at: datetime,
    gap: timedelta,
    deadline_at: datetime | None,
    exclude_action_id: str | None,
    exclude_slot_key: str | None,
) -> Iterator[datetime]:
    normalized_deadline = _wall(deadline_at)
    timeline = _timeline_union(
        tenant_id=tenant_id,
        account_id=account_id,
        start_at=desired_at - gap,
        end_at=normalized_deadline + gap if normalized_deadline else None,
        exclude_action_id=exclude_action_id,
        exclude_slot_key=exclude_slot_key,
    )
    cursor: datetime | None = None
    while True:
        statement = select(timeline.c.timeline_at).distinct().order_by(timeline.c.timeline_at).limit(TIMELINE_PAGE_SIZE)
        if cursor is not None:
            statement = statement.where(timeline.c.timeline_at > cursor)
        page = [point for value in session.scalars(statement) if (point := _wall(value)) is not None]
        if not page:
            return
        yield from page
        if len(page) < TIMELINE_PAGE_SIZE:
            return
        cursor = page[-1]


def _earliest_available_time(
    desired_at: datetime,
    timeline: Iterable[datetime],
    gap: timedelta,
) -> datetime | None:
    candidate = desired_at
    seen = False
    for point in timeline:
        seen = True
        if point + gap <= candidate:
            continue
        if candidate + gap <= point:
            break
        candidate = point + gap
    return candidate if seen else None


def _before_deadline(value: datetime, deadline: datetime) -> bool:
    normalized = _wall(deadline)
    return normalized is not None and value < normalized


__all__ = [
    "ACCOUNT_SOFT_PACING_POLICY_VERSION",
    "AccountPacingDeadlineExceeded",
    "AccountPacingLockUnavailable",
    "PacingClaimDecision",
    "account_policy_not_before",
    "bind_account_pacing_reservation",
    "bind_account_pacing_reservation_for_slot",
    "effective_claim_at",
    "lock_account_pacing",
    "revalidate_action_pacing_before_claim",
    "reserve_account_pacing",
]
