from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ExecutionAttempt,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now

from .daily_coverage_schedule import daily_coverage_due_debt
from .daily_coverage_readiness import refresh_rows


TERMINAL_PRECONFIRMATION_STATUSES = frozenset({"failed", "skipped", "retryable_failed"})


@dataclass(frozen=True)
class DailyCoverageSyncResult:
    coverage_date: date
    created: int
    refreshed: int


def ensure_task_daily_coverage(
    session: Session,
    task: Task,
    *,
    now: datetime | None = None,
    account_ids: list[int] | None = None,
    incremental: bool = False,
) -> DailyCoverageSyncResult:
    timestamp = now or _now()
    scope_materialized = (
        account_ids is None
        and not incremental
        and _daily_scope_materialized(session, task, timestamp.date())
    )
    if scope_materialized:
        release_terminal_coverage_reservations(session, task, timestamp.date())
        return DailyCoverageSyncResult(coverage_date=timestamp.date(), created=0, refreshed=0)
    items = _scope_items(session, task, account_ids)
    if not items:
        return DailyCoverageSyncResult(coverage_date=timestamp.date(), created=0, refreshed=0)
    group = _task_group(session, task)
    coverage_date = _target_date(group, timestamp, incremental=incremental)
    existing = _existing_rows(session, task, coverage_date, [item.account_id for item in items])
    created = 0
    rows_and_items: list[tuple[TaskAccountDailyCoverage, TaskMembershipAdmissionItem]] = []
    for item in items:
        row = existing.get(item.account_id)
        if row is None:
            row = _new_coverage(task, group, item, coverage_date, timestamp)
            session.add(row)
            existing[item.account_id] = row
            created += 1
        rows_and_items.append((row, item))
    refresh_rows(session, rows_and_items, group, timestamp)
    session.flush()
    return DailyCoverageSyncResult(coverage_date=coverage_date, created=created, refreshed=len(items))


def release_terminal_coverage_reservations(session: Session, task: Task, coverage_date: date) -> int:
    rows = list(session.execute(
        select(TaskAccountDailyCoverage, Action)
        .join(Action, Action.id == TaskAccountDailyCoverage.reserved_action_id)
        .where(
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.coverage_date == coverage_date,
            TaskAccountDailyCoverage.state.in_(("reserved", "sending")),
            Action.status.in_(TERMINAL_PRECONFIRMATION_STATUSES),
        )
    ))
    return _release_terminal_rows(session, rows)


def recover_terminal_coverage_reservations(
    session: Session,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> int:
    coverage_date = (now or _now()).date()
    rows = list(session.execute(
        select(TaskAccountDailyCoverage, Action)
        .join(Action, Action.id == TaskAccountDailyCoverage.reserved_action_id)
        .where(
            TaskAccountDailyCoverage.coverage_date == coverage_date,
            TaskAccountDailyCoverage.state.in_(("reserved", "sending")),
            Action.status.in_(TERMINAL_PRECONFIRMATION_STATUSES),
        )
        .order_by(TaskAccountDailyCoverage.updated_at.asc(), TaskAccountDailyCoverage.id.asc())
        .limit(max(1, int(limit)))
        .with_for_update(skip_locked=True)
    ))
    return _release_terminal_rows(session, rows)


def _release_terminal_rows(session: Session, rows) -> int:
    released = 0
    for coverage, action in rows:
        result = action.result if isinstance(action.result, dict) else {}
        coverage.state = "ready"
        coverage.reserved_action_id = None
        coverage.blocker_code = str(result.get("error_code") or action.status)
        coverage.blocker_detail = str(result.get("error_message") or "")
        coverage.next_eligible_at = None
        coverage.updated_at = _now()
        released += 1
    if released:
        session.flush()
    return released


def reserve_coverage_for_action(
    session: Session,
    coverage_id: str,
    action_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.id == coverage_id,
            TaskAccountDailyCoverage.state == "ready",
            TaskAccountDailyCoverage.reserved_action_id.is_(None),
            TaskAccountDailyCoverage.confirmed_count < TaskAccountDailyCoverage.target_count,
        )
        .values(
            state="reserved",
            reserved_action_id=action_id,
            blocker_code="",
            blocker_detail="",
            updated_at=now or _now(),
        )
    )
    return result.rowcount == 1


def release_coverage_reservation(
    session: Session,
    coverage_id: str,
    action_id: str,
    *,
    blocker_code: str,
    blocker_detail: str = "",
    next_eligible_at: datetime | None = None,
) -> bool:
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.id == coverage_id,
            TaskAccountDailyCoverage.reserved_action_id == action_id,
            TaskAccountDailyCoverage.state.in_(("reserved", "sending")),
        )
        .values(
            state="ready",
            reserved_action_id=None,
            blocker_code=blocker_code,
            blocker_detail=blocker_detail,
            next_eligible_at=next_eligible_at,
            updated_at=_now(),
        )
    )
    return result.rowcount == 1


def confirm_coverage_from_attempt(
    session: Session,
    coverage_id: str,
    action_id: str,
    attempt: ExecutionAttempt | None,
) -> bool:
    if attempt is None or attempt.status != "success" or not str(attempt.remote_message_id or "").strip():
        return False
    row = session.scalar(
        select(TaskAccountDailyCoverage)
        .where(TaskAccountDailyCoverage.id == coverage_id)
        .with_for_update()
    )
    if row is None or row.reserved_action_id != action_id:
        return False
    if row.last_success_action_id == action_id:
        return True
    row.confirmed_count = min(row.target_count, row.confirmed_count + 1)
    row.last_success_action_id = action_id
    row.last_remote_message_id = str(attempt.remote_message_id)
    row.reserved_action_id = None
    row.blocker_code = ""
    row.blocker_detail = ""
    row.updated_at = _now()
    if row.confirmed_count >= row.target_count:
        row.state = "confirmed"
        row.completed_at = _now()
    else:
        row.state = "ready"
    return True


def mark_coverage_unknown(
    session: Session,
    coverage_id: str,
    action_id: str,
    *,
    blocker_code: str,
    blocker_detail: str,
) -> bool:
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.id == coverage_id,
            TaskAccountDailyCoverage.reserved_action_id == action_id,
            TaskAccountDailyCoverage.state.in_(("reserved", "sending", "unknown")),
        )
        .values(
            state="unknown",
            blocker_code=blocker_code,
            blocker_detail=blocker_detail,
            updated_at=_now(),
        )
    )
    return result.rowcount == 1


def block_coverage_accounts(
    session: Session,
    task: Task,
    account_ids: list[int],
    *,
    blocker_code: str,
    blocker_detail: str,
    next_eligible_at: datetime,
) -> int:
    if not account_ids:
        return 0
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.coverage_date == _now().date(),
            TaskAccountDailyCoverage.account_id.in_(account_ids),
            TaskAccountDailyCoverage.confirmed_count < TaskAccountDailyCoverage.target_count,
            TaskAccountDailyCoverage.state.in_(("ready", "blocked")),
        )
        .values(
            state="blocked",
            blocker_code=blocker_code,
            blocker_detail=blocker_detail,
            next_eligible_at=next_eligible_at,
            updated_at=_now(),
        )
    )
    return int(result.rowcount or 0)


def release_online_coverage_blockers(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    now: datetime | None = None,
) -> int:
    timestamp = now or _now()
    sendable_membership = select(TgGroupAccount.id).where(
        TgGroupAccount.tenant_id == tenant_id,
        TgGroupAccount.group_id == TaskAccountDailyCoverage.group_id,
        TgGroupAccount.account_id == account_id,
        TgGroupAccount.can_send.is_(True),
    ).exists()
    result = session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.tenant_id == tenant_id,
            TaskAccountDailyCoverage.account_id == account_id,
            TaskAccountDailyCoverage.coverage_date == timestamp.date(),
            TaskAccountDailyCoverage.state == "blocked",
            TaskAccountDailyCoverage.blocker_code == "account_offline",
            TaskAccountDailyCoverage.confirmed_count < TaskAccountDailyCoverage.target_count,
            sendable_membership,
        )
        .values(
            state="ready",
            blocker_code="",
            blocker_detail="",
            next_eligible_at=None,
            updated_at=timestamp,
        )
    )
    return int(result.rowcount or 0)


def backfill_daily_coverage_confirmations(
    session: Session,
    task: Task,
    coverage_date: date,
) -> int:
    rows = list(session.scalars(select(TaskAccountDailyCoverage).where(
        TaskAccountDailyCoverage.task_id == task.id,
        TaskAccountDailyCoverage.coverage_date == coverage_date,
    )))
    if not rows:
        return 0
    start = datetime.combine(coverage_date, datetime.min.time())
    end = start + timedelta(days=1)
    attempts = session.execute(
        select(
            Action.id,
            Action.account_id,
            Action.executed_at,
            ExecutionAttempt.remote_message_id,
        )
        .join(ExecutionAttempt, ExecutionAttempt.action_id == Action.id)
        .where(
            Action.task_id == task.id,
            Action.action_type == "send_message",
            Action.status == "success",
            Action.executed_at >= start,
            Action.executed_at < end,
            ExecutionAttempt.status == "success",
            ExecutionAttempt.remote_message_id != "",
        )
        .order_by(Action.executed_at.asc(), ExecutionAttempt.attempt_no.asc())
    )
    successes = _successes_by_account(attempts)
    return sum(_apply_backfilled_successes(row, successes.get(row.account_id, [])) for row in rows)


def _successes_by_account(attempts) -> dict[int, list[tuple[str, datetime, str]]]:
    grouped: dict[int, dict[str, tuple[str, datetime, str]]] = {}
    for action_id, account_id, executed_at, remote_message_id in attempts:
        if account_id is None or executed_at is None or not str(remote_message_id or "").strip():
            continue
        grouped.setdefault(int(account_id), {})[str(action_id)] = (
            str(action_id), executed_at, str(remote_message_id),
        )
    return {account_id: list(actions.values()) for account_id, actions in grouped.items()}


def _apply_backfilled_successes(
    row: TaskAccountDailyCoverage,
    successes: list[tuple[str, datetime, str]],
) -> int:
    if not successes:
        return 0
    observed = min(row.target_count, len(successes))
    if observed < row.confirmed_count:
        return 0
    if observed == row.confirmed_count and row.last_success_action_id:
        return 0
    confirmed = max(row.confirmed_count, observed)
    action_id, executed_at, remote_message_id = successes[-1]
    changed = row.confirmed_count != confirmed or row.last_success_action_id != action_id
    row.confirmed_count = confirmed
    row.last_success_action_id = action_id
    row.last_remote_message_id = remote_message_id
    if confirmed >= row.target_count:
        row.state = "confirmed"
        row.completed_at = executed_at
        row.reserved_action_id = None
        row.blocker_code = ""
        row.blocker_detail = ""
    row.updated_at = _now()
    return int(changed)


def ready_coverage_rows(
    session: Session,
    task: Task,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[TaskAccountDailyCoverage]:
    timestamp = now or _now()
    stmt = _ready_coverage_stmt(task, timestamp)
    if limit is not None:
        stmt = stmt.limit(max(1, int(limit)))
    return list(session.scalars(stmt))


def ready_coverage_rows_by_account(
    session: Session,
    task: Task,
    account_ids: list[int],
    *,
    now: datetime | None = None,
) -> dict[int, TaskAccountDailyCoverage]:
    wanted = set(account_ids)
    if not wanted:
        return {}
    return {
        row.account_id: row
        for row in ready_coverage_rows(session, task, now=now)
        if row.account_id in wanted
    }


def ready_coverage_remaining_count(session: Session, task: Task, *, now: datetime | None = None) -> int:
    return sum(max(0, row.target_count - row.confirmed_count) for row in ready_coverage_rows(session, task, now=now))


def _ready_coverage_stmt(task: Task, timestamp: datetime):
    return (
        select(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.coverage_date == timestamp.date(),
            TaskAccountDailyCoverage.state == "ready",
            TaskAccountDailyCoverage.confirmed_count < TaskAccountDailyCoverage.target_count,
            (
                TaskAccountDailyCoverage.next_eligible_at.is_(None)
                | (TaskAccountDailyCoverage.next_eligible_at <= timestamp)
            ),
        )
        .order_by(
            TaskAccountDailyCoverage.next_eligible_at.asc().nullsfirst(),
            TaskAccountDailyCoverage.targeted_at.asc(),
            TaskAccountDailyCoverage.account_id.asc(),
        )
    )


def _task_group(session: Session, task: Task) -> TgGroup:
    group_id = int((task.type_config or {}).get("target_group_id") or 0)
    group = session.get(TgGroup, group_id) if group_id else None
    if group is None or group.tenant_id != task.tenant_id:
        raise ValueError("all-account coverage task target group not found")
    return group


def _scope_items(session: Session, task: Task, account_ids: list[int] | None) -> list[TaskMembershipAdmissionItem]:
    stmt = select(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.task_id == task.id)
    if account_ids is not None:
        stmt = stmt.where(TaskMembershipAdmissionItem.account_id.in_(account_ids))
    return list(session.scalars(stmt.order_by(TaskMembershipAdmissionItem.account_id.asc())))


def _daily_scope_materialized(session: Session, task: Task, coverage_date: date) -> bool:
    relation_count = session.scalar(
        select(func.count(TaskMembershipAdmissionItem.id)).where(
            TaskMembershipAdmissionItem.task_id == task.id,
        )
    ) or 0
    if relation_count == 0:
        return True
    coverage_count = session.scalar(
        select(func.count(TaskAccountDailyCoverage.id)).where(
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.coverage_date == coverage_date,
        )
    ) or 0
    return int(coverage_count) >= int(relation_count)


def _existing_rows(
    session: Session,
    task: Task,
    coverage_date: date,
    account_ids: list[int],
) -> dict[int, TaskAccountDailyCoverage]:
    if not account_ids:
        return {}
    rows = session.scalars(
        select(TaskAccountDailyCoverage).where(
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.coverage_date == coverage_date,
            TaskAccountDailyCoverage.account_id.in_(account_ids),
        )
    )
    return {row.account_id: row for row in rows}


def _new_coverage(
    task: Task,
    group: TgGroup,
    item: TaskMembershipAdmissionItem,
    coverage_date: date,
    timestamp: datetime,
) -> TaskAccountDailyCoverage:
    target_count = max(1, int((task.type_config or {}).get("per_account_daily_min_messages") or 1))
    return TaskAccountDailyCoverage(
        tenant_id=task.tenant_id,
        task_id=task.id,
        group_id=group.id,
        account_id=item.account_id,
        membership_item_id=item.id,
        coverage_date=coverage_date,
        target_count=target_count,
        targeted_at=timestamp,
    )


def _target_date(group: TgGroup, timestamp: datetime, *, incremental: bool) -> date:
    if not incremental:
        return timestamp.date()
    end_hour, end_minute = _window_end(group.active_window)
    end = timestamp.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    return timestamp.date() + timedelta(days=1) if timestamp >= end else timestamp.date()


def _window_end(active_window: str) -> tuple[int, int]:
    try:
        end_raw = str(active_window or "09:00-23:00").split("-", 1)[1]
        hour, minute = end_raw.split(":", 1)
        return int(hour), int(minute)
    except (IndexError, TypeError, ValueError):
        raise ValueError(f"invalid group active window: {active_window}")


__all__ = [
    "backfill_daily_coverage_confirmations",
    "block_coverage_accounts",
    "DailyCoverageSyncResult",
    "confirm_coverage_from_attempt",
    "daily_coverage_due_debt",
    "ensure_task_daily_coverage",
    "ready_coverage_remaining_count",
    "ready_coverage_rows",
    "ready_coverage_rows_by_account",
    "mark_coverage_unknown",
    "recover_terminal_coverage_reservations",
    "release_online_coverage_blockers",
    "release_coverage_reservation",
    "release_terminal_coverage_reservations",
    "reserve_coverage_for_action",
]
