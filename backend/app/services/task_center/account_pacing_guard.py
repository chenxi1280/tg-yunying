from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AccountPacingReservation,
    Action,
    Task,
    TgAccount,
)
from .account_pacing_timeline import (
    OPEN_GUARD_STATUSES as _OPEN_GUARD_STATUSES,
    account_timeline_points as _account_timeline_points,
    earliest_available_time as _earliest_available_time,
)
from .account_pacing_policy import (
    account_not_before as _account_not_before,
    account_policy_not_before,
    task_policy_not_before,
    wall_time as _wall,
)
from .account_pacing_reservations import (
    ACCOUNT_SOFT_PACING_POLICY_VERSION,
    ACCOUNT_BEHAVIOR_SESSION_PACING_POLICY_VERSION,
    ACCOUNT_BEHAVIOR_SESSION_WAKE_POLICY_VERSION,
    ACCOUNT_BEHAVIOR_SESSION_WAKE_CONSUMED_POLICY_VERSION,
    new_account_pacing_reservation as _new_reservation,
    normalize_reservation_window as _normalize_reservation_window,
    bind_account_pacing_reservation,
    bind_account_pacing_reservation_for_slot,
    release_unbound_account_pacing_reservation,
    release_safe_task_account_pacing_reservations,
    reservation_for_any_slot as _reservation_for_any_slot,
    reservation_for_slot as _reservation_for_slot,
)
from .engagement_action_classes import action_class_for_type
from .engagement_behavior_sessions import (
    behavior_session_not_before,
    behavior_session_wake_available,
    reserve_behavior_session_wake,
)
from .account_pacing_claim_outcome import PacingClaimDecision, _settle_claim_pacing
from .account_pacing_window import ReservationTiming, effective_claim_at, resolve_windowed_timing


_OPEN_RESERVATION_STATES = ("reserved", "bound")


class AccountPacingDeadlineExceeded(RuntimeError):
    pass


class AccountPacingLockUnavailable(RuntimeError):
    pass




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


def lock_task_pacing(session: Session, task_id: str) -> None:
    """群级（任务级）节奏锁：串行化同任务的 claim 校验。

    2026-08-17 线上验证发现跨账号并行 claim 时，两个事务互相看不到对方
    未提交的 claiming 状态，群级 timeline 同时"干净"导致同秒突发；本锁
    保证同任务同一时刻只有一个事务在执行群级校验+claim。
    """
    statement = select(Task.id).where(Task.id == task_id)
    if session.get_bind().dialect.name == "sqlite":
        if session.scalar(statement) is None:
            raise ValueError("task_pacing_task_not_found")
        return
    if session.scalar(statement.with_for_update(skip_locked=True)) is not None:
        return
    if session.scalar(statement) is None:
        raise ValueError("task_pacing_task_not_found")
    raise AccountPacingLockUnavailable("task_pacing_lock_busy")


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
    engagement_contract_version: str = "",
    action_class: str = "",
    allow_session_wake: bool = False,
) -> AccountPacingReservation:
    due_at, release_at, deadline_at = _normalize_reservation_window(
        due_at, release_not_before_at, deadline_at,
    )
    lock_account_pacing(session, account_id)
    existing = _reservation_for_any_slot(session, tenant_id, account_id, slot_key)
    if existing is not None:
        return _reuse_existing_reservation(
            session,
            existing,
            due_at=due_at,
            release_at=release_at,
            deadline_at=deadline_at,
        )
    timing, wake_reserved = _reservation_timing_with_optional_wake(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        engagement_contract_version=engagement_contract_version,
        action_class=action_class,
        release_at=release_at,
        deadline_at=deadline_at,
        exclude_slot_key=None,
        allow_session_wake=allow_session_wake,
    )
    reservation = _new_reservation(
        tenant_id=tenant_id,
        task_id=task_id,
        account_id=account_id,
        slot_key=slot_key,
        due_at=due_at,
        timing=timing,
        deadline_at=deadline_at,
        engagement_contract_version=engagement_contract_version,
        action_class=action_class,
        session_wake_reserved=wake_reserved,
    )
    session.add(reservation)
    session.flush()
    return reservation


def _reservation_timing_with_optional_wake(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    engagement_contract_version: str,
    action_class: str,
    release_at: datetime,
    deadline_at: datetime | None,
    exclude_slot_key: str | None,
    allow_session_wake: bool,
) -> tuple[ReservationTiming, bool]:
    wake_allowed = (
        allow_session_wake
        and engagement_contract_version == "unified_engagement_v1"
    )
    if wake_allowed:
        timing = _resolve_reservation_timing(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            engagement_contract_version=engagement_contract_version,
            action_class=action_class,
            release_at=release_at,
            deadline_at=deadline_at,
            exclude_slot_key=exclude_slot_key,
            session_wake_reserved=True,
        )
        if behavior_session_wake_available(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            desired_at=release_at,
            pending_policy_version=ACCOUNT_BEHAVIOR_SESSION_WAKE_POLICY_VERSION,
        ):
            return timing, True
    timing = _resolve_reservation_timing(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        engagement_contract_version=engagement_contract_version,
        action_class=action_class,
        release_at=release_at,
        deadline_at=deadline_at,
        exclude_slot_key=exclude_slot_key,
    )
    return timing, False


def _resolve_reservation_timing(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    engagement_contract_version: str,
    action_class: str,
    release_at: datetime,
    deadline_at: datetime | None,
    exclude_slot_key: str | None,
    session_wake_reserved: bool = False,
) -> ReservationTiming:
    def window_floor(at: datetime) -> datetime | None:
        if session_wake_reserved:
            return at
        return behavior_session_not_before(
            session, tenant_id=tenant_id,
            engagement_contract_version=engagement_contract_version,
            account_id=account_id, desired_at=at, deadline_at=deadline_at,
        )

    def timeline_floor(at: datetime) -> datetime | None:
        return _account_not_before(
            session, tenant_id=tenant_id, account_id=account_id,
            action_class=action_class,
            use_pair_policy=engagement_contract_version == "unified_engagement_v1",
            now_value=at, deadline_at=deadline_at,
            exclude_action_id=None, exclude_slot_key=exclude_slot_key,
            include_planned=True,
        )

    timing = resolve_windowed_timing(
        desired_at=release_at, deadline_at=deadline_at,
        window_not_before=window_floor, timeline_not_before=timeline_floor,
    )
    if timing.failure_code:
        raise AccountPacingDeadlineExceeded(timing.failure_code)
    return timing



def _reuse_existing_reservation(
    session: Session,
    reservation: AccountPacingReservation,
    *,
    due_at: datetime,
    release_at: datetime,
    deadline_at: datetime | None,
) -> AccountPacingReservation:
    if reservation.state == "released" and reservation.action_id is None:
        return _rearm_available_reservation(
            session,
            reservation,
            due_at=due_at,
            release_at=release_at,
            deadline_at=deadline_at,
        )
    if reservation.state not in _OPEN_RESERVATION_STATES:
        if reservation.state == "missed":
            raise AccountPacingDeadlineExceeded("pacing_slot_already_missed")
        raise ValueError("account_pacing_reservation_state_invalid")
    if reservation.action_id is not None:
        return reservation
    return _rearm_available_reservation(
        session,
        reservation,
        due_at=due_at,
        release_at=release_at,
        deadline_at=deadline_at,
    )


def _rearm_available_reservation(
    session: Session,
    reservation: AccountPacingReservation,
    *,
    due_at: datetime,
    release_at: datetime,
    deadline_at: datetime | None,
) -> AccountPacingReservation:
    unified = _uses_behavior_session_policy(reservation.policy_version)
    timing = _resolve_reservation_timing(
        session,
        tenant_id=reservation.tenant_id,
        account_id=reservation.account_id,
        engagement_contract_version="unified_engagement_v1" if unified else "",
        action_class=reservation.action_class or "",
        release_at=release_at,
        deadline_at=deadline_at,
        exclude_slot_key=reservation.pacing_slot_key,
        session_wake_reserved=(
            reservation.policy_version == ACCOUNT_BEHAVIOR_SESSION_WAKE_POLICY_VERSION
        ),
    )
    reservation.state = "reserved"
    reservation.due_at = due_at
    reservation.release_not_before_at = timing.release_at
    reservation.effective_claim_at = timing.effective_at
    reservation.source_deadline_at = deadline_at
    reservation.version = int(reservation.version or 1) + 1
    return reservation


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
        return PacingClaimDecision(False, action.scheduled_at, "account_pacing_lock_busy")
    desired_at, reservation = _claim_desired_and_reservation(
        session, action, now_value,
    )
    try:
        timing, group_conflict = _claim_windowed_timing(
            session, action, reservation=reservation, desired_at=desired_at,
        )
    except AccountPacingLockUnavailable:
        return PacingClaimDecision(False, action.scheduled_at, "task_pacing_lock_busy")
    if timing.failure_code:
        return PacingClaimDecision(False, timing.effective_at, "pacing_claim_deadline_exceeded")
    return _settle_claim_pacing(
        action, reservation, now_value=now_value, desired_at=timing.release_at,
        not_before=timing.effective_at, group_conflict=group_conflict,
    )


def _claim_windowed_timing(
    session: Session,
    action: Action,
    *,
    reservation: AccountPacingReservation,
    desired_at: datetime,
) -> tuple[ReservationTiming, bool]:
    group_conflict = False

    def timeline_floor(at: datetime) -> datetime | None:
        nonlocal group_conflict
        account_floor = _claim_account_not_before(session, action, reservation, at)
        not_before, group_conflict = _claim_group_not_before(
            session, action, reservation, at, account_floor,
        )
        return not_before

    timing = resolve_windowed_timing(
        desired_at=desired_at, deadline_at=_wall(reservation.source_deadline_at),
        window_not_before=lambda at: _claim_session_floor(session, action, reservation, at),
        timeline_not_before=timeline_floor,
    )
    return timing, group_conflict


def _claim_desired_and_reservation(
    session: Session,
    action: Action,
    now_value: datetime,
) -> tuple[datetime, AccountPacingReservation]:
    desired_at = max(value for value in (
        _wall(now_value),
        _wall(action.pacing_due_at),
        _wall(action.release_not_before_at),
    ) if value is not None)
    reservation = _reservation_for_slot(
        session, action.tenant_id, int(action.account_id), str(action.pacing_slot_key),
    )
    if reservation is None:
        raise ValueError("account_pacing_reservation_missing")
    return desired_at, reservation


def _claim_session_floor(
    session: Session,
    action: Action,
    reservation: AccountPacingReservation,
    desired_at: datetime,
) -> datetime | None:
    if reservation.policy_version == ACCOUNT_BEHAVIOR_SESSION_WAKE_CONSUMED_POLICY_VERSION:
        return desired_at
    if reservation.policy_version == ACCOUNT_BEHAVIOR_SESSION_WAKE_POLICY_VERSION:
        if reserve_behavior_session_wake(
            session,
            tenant_id=action.tenant_id,
            account_id=int(action.account_id),
            desired_at=desired_at,
        ):
            reservation.policy_version = (
                ACCOUNT_BEHAVIOR_SESSION_WAKE_CONSUMED_POLICY_VERSION
            )
            reservation.version = int(reservation.version or 1) + 1
            return desired_at
    return behavior_session_not_before(
        session,
        tenant_id=action.tenant_id,
        engagement_contract_version=(
            "unified_engagement_v1"
            if _uses_behavior_session_policy(reservation.policy_version)
            else ""
        ),
        account_id=int(action.account_id),
        desired_at=desired_at,
        deadline_at=reservation.source_deadline_at,
    )


def _claim_account_not_before(
    session: Session,
    action: Action,
    reservation: AccountPacingReservation,
    desired_at: datetime,
) -> datetime | None:
    return _account_not_before(
        session,
        tenant_id=action.tenant_id,
        account_id=int(action.account_id),
        action_class=(
            reservation.action_class
            or action_class_for_type(str(action.action_type or ""))
        ),
        use_pair_policy=(
            _uses_behavior_session_policy(reservation.policy_version)
        ),
        now_value=desired_at,
        deadline_at=reservation.source_deadline_at,
        exclude_action_id=action.id,
        exclude_slot_key=str(action.pacing_slot_key),
        include_planned=False,
    )


def _uses_behavior_session_policy(policy_version: str) -> bool:
    return policy_version in {
        ACCOUNT_BEHAVIOR_SESSION_PACING_POLICY_VERSION,
        ACCOUNT_BEHAVIOR_SESSION_WAKE_POLICY_VERSION,
        ACCOUNT_BEHAVIOR_SESSION_WAKE_CONSUMED_POLICY_VERSION,
    }


def _claim_group_not_before(
    session: Session,
    action: Action,
    reservation: AccountPacingReservation,
    desired_at: datetime,
    account_not_before: datetime | None,
) -> tuple[datetime | None, bool]:
    if str(action.task_type or "") != "group_ai_chat":
        return account_not_before, False
    lock_task_pacing(session, str(action.task_id))
    group_gap = timedelta(
        seconds=max(1, int(get_settings().ai_group_send_pacing_min_gap_seconds))
    )
    group_not_before = task_policy_not_before(
        session,
        str(action.task_id),
        tenant_id=action.tenant_id,
        desired_at=desired_at,
        gap=group_gap,
        deadline_at=reservation.source_deadline_at,
        exclude_action_id=action.id,
        exclude_slot_key=str(action.pacing_slot_key),
        include_planned=False,
    )
    if group_not_before is None or (
        account_not_before is not None and group_not_before <= account_not_before
    ):
        return account_not_before, False
    return group_not_before, True




__all__ = [
    "ACCOUNT_SOFT_PACING_POLICY_VERSION",
    "ACCOUNT_BEHAVIOR_SESSION_WAKE_CONSUMED_POLICY_VERSION",
    "ACCOUNT_BEHAVIOR_SESSION_WAKE_POLICY_VERSION",
    "AccountPacingDeadlineExceeded",
    "AccountPacingLockUnavailable",
    "PacingClaimDecision",
    "account_policy_not_before",
    "bind_account_pacing_reservation",
    "bind_account_pacing_reservation_for_slot",
    "effective_claim_at",
    "lock_account_pacing",
    "release_unbound_account_pacing_reservation",
    "release_safe_task_account_pacing_reservations",
    "revalidate_action_pacing_before_claim",
    "reserve_account_pacing",
]
