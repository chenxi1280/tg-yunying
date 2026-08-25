from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, union_all
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AccountPacingReservation,
    Action,
    ExecutionAttempt,
    FulfillmentRemoteFact,
    Task,
    TgAccount,
)
from app.services._common import _now
from app.timezone import BEIJING_TZ

from .source_pacing import latest_wall_datetime, wall_datetime


ACCOUNT_SOFT_PACING_POLICY_VERSION = "account_soft_pacing_v1"
TIMELINE_PAGE_SIZE = 128
_OPEN_GUARD_STATUSES = ("pending", "claiming", "executing", "retryable_failed", "unknown_after_send")
# claim 层只认在途/事实：pending 计划与预约不算已占发送点。2026-08-17 生产事故：
# 积压 ready pending（含各自预约）排成每秒 1 条的密集计划队列，pending 互相把
# claim 推到队尾形成互锁，发送坍塌；实际间隔由任务行锁 + claiming 在途点保证。
_INFLIGHT_GUARD_STATUSES = ("claiming", "executing", "retryable_failed", "unknown_after_send")
_OPEN_RESERVATION_STATES = ("reserved", "bound")
_REUSABLE_TERMINAL_ACTION_STATUSES = frozenset({"failed", "skipped"})


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


def account_policy_not_before(
    session: Session,
    account_id: int,
    *,
    tenant_id: int,
    now_value: datetime | None = None,
    deadline_at: datetime | None = None,
    exclude_action_id: str | None = None,
    exclude_slot_key: str | None = None,
    include_planned: bool = True,
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
        include_planned=include_planned,
    )
    return _earliest_available_time(desired_at, points, gap)


def task_policy_not_before(
    session: Session,
    task_id: str,
    *,
    tenant_id: int,
    desired_at: datetime,
    gap: timedelta,
    deadline_at: datetime | None = None,
    exclude_action_id: str | None = None,
    exclude_slot_key: str | None = None,
    include_planned: bool = True,
) -> datetime | None:
    """任务级（≈群级）发送时间线：同一 Task 内所有账号共享的最小间隔。

    2026-08-17 生产节奏诊断：账号级 pacing 只约束单账号，跨账号在同一个
    claim 周期并行发送形成同秒 5-19 条突发；本门禁在 claim 时把同任务
    最近的 open 发送点也纳入时间线。
    """
    points = _account_timeline_points(
        session,
        tenant_id,
        None,
        desired_at=desired_at,
        gap=gap,
        deadline_at=deadline_at,
        exclude_action_id=exclude_action_id,
        exclude_slot_key=exclude_slot_key,
        task_id=task_id,
        include_planned=include_planned,
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
    release_at = latest_wall_datetime(due_at, release_not_before_at)
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


def _reuse_existing_reservation(
    session: Session,
    reservation: AccountPacingReservation,
    *,
    due_at: datetime,
    release_at: datetime,
    deadline_at: datetime | None,
) -> AccountPacingReservation:
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
    not_before = account_policy_not_before(
        session,
        reservation.account_id,
        tenant_id=reservation.tenant_id,
        now_value=release_at,
        deadline_at=deadline_at,
        exclude_slot_key=reservation.pacing_slot_key,
    )
    effective_at = effective_claim_at(release_at, not_before)
    if deadline_at is not None and not _before_deadline(effective_at, deadline_at):
        raise AccountPacingDeadlineExceeded("account_timeline_conflict")
    reservation.state = "reserved"
    reservation.due_at = due_at
    reservation.release_not_before_at = release_at
    reservation.effective_claim_at = effective_at
    reservation.source_deadline_at = deadline_at
    reservation.version = int(reservation.version or 1) + 1
    return reservation


def release_safe_task_account_pacing_reservations(
    session: Session,
    task: Task,
) -> int:
    session.flush()
    statement = select(AccountPacingReservation).where(
        AccountPacingReservation.task_id == task.id,
        AccountPacingReservation.state.in_(_OPEN_RESERVATION_STATES),
        (
            AccountPacingReservation.action_id.is_not(None)
            | (AccountPacingReservation.state == "bound")
        ),
    )
    if session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update(of=AccountPacingReservation)
    reservations = list(session.scalars(statement))
    action_ids = [row.action_id for row in reservations if row.action_id]
    actions = (
        {
            row.id: row
            for row in session.scalars(select(Action).where(Action.id.in_(action_ids)))
        }
        if action_ids
        else {}
    )
    remote_bound = _remote_bound_action_ids(session, action_ids)
    released = 0
    for reservation in reservations:
        action = actions.get(str(reservation.action_id or ""))
        if not _reservation_reusable(action, remote_bound):
            continue
        reservation.action_id = None
        reservation.state = "reserved"
        reservation.version = int(reservation.version or 1) + 1
        released += 1
    return released


def _remote_bound_action_ids(session: Session, action_ids: list[str]) -> set[str]:
    if not action_ids:
        return set()
    attempts = set(
        session.scalars(
            select(ExecutionAttempt.action_id).where(
                ExecutionAttempt.action_id.in_(action_ids),
                (ExecutionAttempt.gateway_call_started_at.is_not(None))
                | (ExecutionAttempt.remote_message_id != ""),
            )
        )
    )
    facts = set(
        session.scalars(
            select(FulfillmentRemoteFact.action_id).where(
                FulfillmentRemoteFact.action_id.in_(action_ids),
            )
        )
    )
    return attempts | facts


def _reservation_reusable(
    action: Action | None,
    remote_bound: set[str],
) -> bool:
    if action is None:
        return True
    if action.status not in _REUSABLE_TERMINAL_ACTION_STATUSES:
        return False
    result = dict(action.result or {})
    return (
        action.id not in remote_bound
        and not result.get("gateway_call_started_at")
        and not result.get("remote_message_id")
    )


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
        include_planned=False,
    )
    group_conflict = False
    if str(action.task_type or "") == "group_ai_chat":
        try:
            lock_task_pacing(session, str(action.task_id))
        except AccountPacingLockUnavailable:
            return PacingClaimDecision(
                False,
                action.scheduled_at,
                "task_pacing_lock_busy",
            )
        group_gap = timedelta(seconds=max(1, int(get_settings().ai_group_send_pacing_min_gap_seconds)))
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
        if group_not_before is not None and (not_before is None or group_not_before > not_before):
            not_before = group_not_before
            group_conflict = True
    effective_at = effective_claim_at(desired_at, not_before)
    if reservation.source_deadline_at and not _before_deadline(
        effective_at, reservation.source_deadline_at,
    ):
        return PacingClaimDecision(False, effective_at, "pacing_claim_deadline_exceeded")
    if effective_at <= _wall(now_value):
        _sync_claim_time(action, reservation, effective_at)
        return PacingClaimDecision(True, effective_at)
    reason = "group_send_pacing_conflict" if group_conflict else "account_timeline_conflict"
    _defer_action_claim(action, reservation, effective_at, reason_code=reason)
    return PacingClaimDecision(False, effective_at, reason)


def _defer_action_claim(
    action: Action,
    reservation: AccountPacingReservation,
    effective_at: datetime,
    *,
    reason_code: str = "account_timeline_conflict",
) -> None:
    action.scheduled_at = effective_at
    action.effective_claim_at = effective_at
    action.action_version = int(action.action_version or 1) + 1
    action.result = {
        **(action.result or {}),
        "claim_pacing_deferred": {
            "reason_code": reason_code,
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
    # claim 放行即占位：scheduled_at 锚定到 claim 时刻，使 claiming 在途点
    # 进入时间线窗口（start_at = now - gap）。不更新会保留过期老值，同批
    # 后续 claim 与并发 final gate 都看不见本条在途 → 同秒批量挤发
    # （2026-08-17 部署后线上实测 min gap 0.12s）。
    action.scheduled_at = effective_at
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
    account_id: int | None,
    start_at: datetime,
    end_at: datetime | None,
    exclude_action_id: str | None,
    exclude_slot_key: str | None,
    task_id: str | None = None,
    include_planned: bool = True,
):
    action_scope = (
        Action.task_id == task_id if task_id is not None else Action.account_id == account_id
    )
    reservation_scope = (
        AccountPacingReservation.task_id == task_id
        if task_id is not None
        else AccountPacingReservation.account_id == account_id
    )
    action_filters = [
        Action.tenant_id == tenant_id,
        action_scope,
        Action.status.in_(_OPEN_GUARD_STATUSES if include_planned else _INFLIGHT_GUARD_STATUSES),
        Action.scheduled_at.is_not(None),
        Action.scheduled_at >= start_at,
    ]
    fact_filters = [
        FulfillmentRemoteFact.tenant_id == tenant_id,
        action_scope,
        FulfillmentRemoteFact.fact_kind.in_((
            "remote_message_observed",
            "view_observed",
            "reaction_observed",
        )),
        FulfillmentRemoteFact.observed_at >= start_at,
    ]
    reservation_filters = [
        AccountPacingReservation.tenant_id == tenant_id,
        reservation_scope,
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
    branches = [
        select(Action.scheduled_at.label("timeline_at")).where(*action_filters),
        select(FulfillmentRemoteFact.observed_at.label("timeline_at"))
        .join(Action, Action.id == FulfillmentRemoteFact.action_id)
        .where(*fact_filters),
    ]
    if include_planned:
        branches.append(
            select(AccountPacingReservation.effective_claim_at.label("timeline_at"))
            .where(*reservation_filters)
        )
    return union_all(*branches).subquery()


def _account_timeline_points(
    session: Session,
    tenant_id: int,
    account_id: int | None,
    *,
    desired_at: datetime,
    gap: timedelta,
    deadline_at: datetime | None,
    exclude_action_id: str | None,
    exclude_slot_key: str | None,
    task_id: str | None = None,
    include_planned: bool = True,
) -> Iterator[datetime]:
    normalized_deadline = _wall(deadline_at)
    timeline = _timeline_union(
        tenant_id=tenant_id,
        account_id=account_id,
        start_at=desired_at - gap,
        end_at=normalized_deadline + gap if normalized_deadline else None,
        exclude_action_id=exclude_action_id,
        exclude_slot_key=exclude_slot_key,
        task_id=task_id,
        include_planned=include_planned,
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
    "release_safe_task_account_pacing_reservations",
    "revalidate_action_pacing_before_claim",
    "reserve_account_pacing",
]
