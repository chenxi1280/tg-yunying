"""Allocate AI source slots under the caller's source-row lock."""
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import (
    AccountBehaviorSessionPlan, AccountPacingReservation, Action,
    SourcePacingAdmission, SourcePacingState, Task, TaskDayLedger,
)

from .account_pacing_reservations import (
    ACCOUNT_BEHAVIOR_SESSION_PACING_POLICY_VERSION,
    ACCOUNT_BEHAVIOR_SESSION_WAKE_POLICY_VERSION,
)
from .datetime_compat import utc_storage_as_beijing_wall
from .source_pacing import wall_datetime
from .source_pacing_reservation import SourceAdmissionSpec


OPEN_ACTION_STATUSES = ("pending", "claiming", "executing", "retryable_failed")
SESSION_POLICIES = frozenset((
    ACCOUNT_BEHAVIOR_SESSION_PACING_POLICY_VERSION,
    ACCOUNT_BEHAVIOR_SESSION_WAKE_POLICY_VERSION,
))
UNIFIED_CONTRACT = "unified_engagement_v1"


def ai_source_not_before(
    session: Session,
    action: Action,
    state: SourcePacingState,
    *,
    admission: SourcePacingAdmission,
    spec: SourceAdmissionSpec,
    timestamp: datetime,
) -> datetime:
    desired = max(timestamp, spec.release_at)
    if action.effective_claim_at is not None:
        desired = max(desired, wall_datetime(action.effective_claim_at))
    if state.last_call_started_at is not None:
        gap = max(int(state.last_source_gap_seconds or 0), spec.source_gap_seconds)
        desired = max(desired, wall_datetime(state.last_call_started_at) + timedelta(seconds=gap))
    windows = _action_windows(session, action, deadline=spec.deadline_at, timestamp=timestamp)
    peers = _future_reservations(session, admission, timestamp=timestamp)
    return earliest_source_gap(
        desired, peers, gap_seconds=spec.source_gap_seconds,
        deadline=spec.deadline_at,
        window_floor=lambda at: _window_floor(windows, at),
    )


def earliest_source_gap(
    desired: datetime,
    peers: Iterable[tuple[datetime, int]],
    *,
    gap_seconds: int,
    deadline: datetime,
    window_floor: Callable[[datetime], datetime | None],
) -> datetime:
    candidate = window_floor(desired)
    intervals = sorted(
        (at - timedelta(seconds=max(gap_seconds, peer_gap)),
         at + timedelta(seconds=max(gap_seconds, peer_gap)))
        for at, peer_gap in peers
    )
    for start, end in intervals:
        if candidate is None or candidate >= deadline:
            return deadline
        if candidate <= start:
            return candidate
        if candidate < end:
            candidate = window_floor(end)
    return deadline if candidate is None else min(candidate, deadline)


def _future_reservations(
    session: Session,
    admission: SourcePacingAdmission,
    *,
    timestamp: datetime,
) -> tuple[tuple[datetime, int], ...]:
    rows = session.execute(_reservation_query(admission, timestamp=timestamp)).all()
    deadlines = [_reservation_deadline(row) for row in rows]
    windows = _peer_windows(session, admission, rows=rows, deadlines=deadlines, timestamp=timestamp)
    points = []
    for row, deadline in zip(rows, deadlines):
        other, account_id, config, _, _, policy, release, effective = row
        at = wall_datetime(other.call_not_before_at)
        constraints = (wall_datetime(value) for value in (release, effective) if value is not None)
        if not timestamp <= at < deadline or any(at < value for value in constraints):
            continue
        if _uses_sessions(policy, config) and _window_floor(windows.get(account_id, ()), at) != at:
            continue
        points.append((at, int(other.source_gap_seconds)))
    return tuple(sorted(points))


def _reservation_deadline(row) -> datetime:
    frozen = row.source_deadline_at
    return wall_datetime(frozen) if frozen is not None else utc_storage_as_beijing_wall(row.deadline_at)


def _peer_windows(
    session: Session,
    admission: SourcePacingAdmission,
    *,
    rows,
    deadlines: list[datetime],
    timestamp: datetime,
) -> dict[int, tuple[tuple[datetime, datetime], ...]]:
    account_ids = {row.account_id for row in rows if _uses_sessions(row.policy_version, row.type_config)}
    if not account_ids:
        return {}
    plans = session.scalars(select(AccountBehaviorSessionPlan).where(
        AccountBehaviorSessionPlan.tenant_id == admission.tenant_id,
        AccountBehaviorSessionPlan.account_id.in_(account_ids),
        AccountBehaviorSessionPlan.state == "active",
        AccountBehaviorSessionPlan.task_day >= timestamp.date(),
        AccountBehaviorSessionPlan.task_day <= max(deadlines).date(),
    ))
    windows = {}
    for plan in plans:
        windows.setdefault(plan.account_id, []).extend(_plan_windows(plan))
    return {account_id: tuple(sorted(values)) for account_id, values in windows.items()}


def _reservation_query(admission: SourcePacingAdmission, *, timestamp: datetime):
    return (
        select(SourcePacingAdmission, Action.account_id, Task.type_config, TaskDayLedger.deadline_at,
               AccountPacingReservation.source_deadline_at, AccountPacingReservation.policy_version,
               Action.release_not_before_at, Action.effective_claim_at)
        .join(Action, Action.id == SourcePacingAdmission.action_id)
        .join(Task, Task.id == SourcePacingAdmission.task_id)
        .join(TaskDayLedger, TaskDayLedger.id == SourcePacingAdmission.pacing_period_key)
        .outerjoin(AccountPacingReservation, and_(
            AccountPacingReservation.action_id == Action.id,
            AccountPacingReservation.state.in_(("reserved", "bound")),
        ))
        .where(
            SourcePacingAdmission.source_pacing_state_id == admission.source_pacing_state_id,
            SourcePacingAdmission.tenant_id == admission.tenant_id,
            SourcePacingAdmission.id != admission.id,
            SourcePacingAdmission.state == "reserved",
            SourcePacingAdmission.call_not_before_at >= timestamp,
            SourcePacingAdmission.lifecycle_epoch == Task.task_lifecycle_epoch,
            SourcePacingAdmission.lifecycle_epoch == Action.task_lifecycle_epoch,
            SourcePacingAdmission.pacing_plan_hash == Action.pacing_plan_hash,
            Task.status == "running",
            Action.status.in_(OPEN_ACTION_STATUSES),
            TaskDayLedger.lifecycle_status == "open",
        )
    )


def _action_windows(
    session: Session,
    action: Action,
    *,
    deadline: datetime,
    timestamp: datetime,
) -> tuple[tuple[datetime, datetime], ...] | None:
    reservation = session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.action_id == action.id,
        AccountPacingReservation.state.in_(("reserved", "bound")),
    ).limit(1))
    task = session.get(Task, action.task_id)
    if task is None:
        raise LookupError("pacing_source_task_missing")
    policy = reservation.policy_version if reservation is not None else None
    if not _uses_sessions(policy, task.type_config):
        return None
    plans = session.scalars(select(AccountBehaviorSessionPlan).where(
        AccountBehaviorSessionPlan.tenant_id == action.tenant_id,
        AccountBehaviorSessionPlan.account_id == action.account_id,
        AccountBehaviorSessionPlan.state == "active",
        AccountBehaviorSessionPlan.task_day >= timestamp.date(),
        AccountBehaviorSessionPlan.task_day <= deadline.date(),
    ))
    return tuple(sorted(window for plan in plans for window in _plan_windows(plan)))


def _uses_sessions(policy: str | None, config: dict | None) -> bool:
    if policy is not None:
        return policy in SESSION_POLICIES
    return (config or {}).get("engagement_contract_version") == UNIFIED_CONTRACT


def _plan_windows(plan: AccountBehaviorSessionPlan) -> tuple[tuple[datetime, datetime], ...]:
    return tuple(
        (wall_datetime(datetime.fromisoformat(str(window["start_at"]))),
         wall_datetime(datetime.fromisoformat(str(window["end_at"]))))
        for window in plan.windows or []
    )


def _window_floor(
    windows: tuple[tuple[datetime, datetime], ...] | None,
    candidate: datetime,
) -> datetime | None:
    if windows is None:
        return candidate
    for start, end in windows:
        if candidate < end:
            return max(start, candidate)
    return None
