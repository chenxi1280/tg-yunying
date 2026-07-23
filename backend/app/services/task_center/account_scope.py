from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AccountEligibilityEvent,
    AccountStatus,
    OperationTarget,
    RuntimeMetricSnapshot,
    Task,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.security import decrypt_session
from app.services._common import _now
from app.services.account_usage_policy import apply_operational_account_filters

from .config_normalization import apply_group_ai_account_coverage_defaults


@dataclass(frozen=True)
class ScopeSyncResult:
    task_count: int = 0
    created_relations: int = 0
    eligible_accounts: int = 0


SCOPE_RECONCILE_INTERVAL = timedelta(hours=6)
SCOPE_RECONCILE_METRIC = "task_account_scope.reconcile"
SCOPE_EVENT_RETRY_DELAY = timedelta(minutes=5)


def is_all_accounts_task(task: Task) -> bool:
    selection_mode = str((task.account_config or {}).get("selection_mode") or "all")
    effective_config = apply_group_ai_account_coverage_defaults(
        task.type,
        task.type_config or {},
        task.account_config or {},
    )
    coverage_mode = str(effective_config.get("account_coverage_mode") or "")
    return task.type == "group_ai_chat" and selection_mode == "all" and coverage_mode == "all_accounts_daily"


def eligible_account_ids(session: Session, tenant_id: int) -> list[int]:
    accounts = session.scalars(_eligible_account_stmt(session, tenant_id).order_by(TgAccount.id.asc()))
    return [account.id for account in accounts if _session_is_readable(account)]


def _eligible_account_stmt(session: Session, tenant_id: int):
    stmt = select(TgAccount).where(
        TgAccount.tenant_id == tenant_id,
        TgAccount.deleted_at.is_(None),
        TgAccount.status == AccountStatus.ACTIVE.value,
        TgAccount.session_ciphertext.is_not(None),
        TgAccount.session_ciphertext != "",
    )
    stmt = apply_operational_account_filters(stmt)
    rescue_admin_id = _rescue_admin_account_id(session, tenant_id)
    if rescue_admin_id:
        stmt = stmt.where(TgAccount.id != rescue_admin_id)
    return stmt


def initialize_all_account_task_scope(
    session: Session,
    task: Task,
    *,
    now: datetime | None = None,
) -> ScopeSyncResult:
    if not is_all_accounts_task(task):
        return ScopeSyncResult()
    _normalize_all_account_config(task)
    account_ids = eligible_account_ids(session, task.tenant_id)
    created = _sync_task_relations(session, task, account_ids)
    _ensure_daily_coverage(session, task, account_ids, now=now, incremental=False)
    return ScopeSyncResult(task_count=1, created_relations=created, eligible_accounts=len(account_ids))


def bootstrap_missing_all_account_task_scope(
    session: Session,
    task: Task,
    *,
    now: datetime | None = None,
) -> ScopeSyncResult:
    if not is_all_accounts_task(task) or _scope_exists(session, task):
        return ScopeSyncResult()
    return initialize_all_account_task_scope(session, task, now=now)


def scoped_account_ids(session: Session, task: Task) -> list[int]:
    return list(session.scalars(
        select(TaskMembershipAdmissionItem.account_id)
        .where(TaskMembershipAdmissionItem.task_id == task.id)
        .order_by(TaskMembershipAdmissionItem.account_id.asc())
    ))


def emit_account_eligibility_event(session: Session, account_id: int, event_type: str) -> AccountEligibilityEvent:
    account = session.get(TgAccount, account_id)
    if account is None:
        raise ValueError("account not found")
    event = AccountEligibilityEvent(
        tenant_id=account.tenant_id,
        account_id=account.id,
        event_type=event_type,
    )
    session.add(event)
    session.flush()
    return event


def process_account_eligibility_events(
    session: Session,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> int:
    timestamp = now or _now()
    events = list(
        session.scalars(
            select(AccountEligibilityEvent)
            .where(
                AccountEligibilityEvent.processed_at.is_(None),
                or_(
                    AccountEligibilityEvent.next_attempt_at.is_(None),
                    AccountEligibilityEvent.next_attempt_at <= timestamp,
                ),
            )
            .order_by(AccountEligibilityEvent.occurred_at.asc(), AccountEligibilityEvent.id.asc())
            .limit(max(1, limit))
            .with_for_update(skip_locked=True)
        )
    )
    processed = 0
    for event in events:
        try:
            sync_account_to_all_tasks(session, event.account_id, now=timestamp)
        except Exception as exc:
            event.processing_error = str(exc)
            event.attempt_count += 1
            event.next_attempt_at = timestamp + SCOPE_EVENT_RETRY_DELAY
            continue
        event.processed_at = timestamp
        event.processing_error = ""
        event.next_attempt_at = None
        processed += 1
    session.flush()
    return processed


def drain_account_scope_events(
    session_factory,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> int:
    with session_factory() as session:
        processed = process_account_eligibility_events(session, limit=limit, now=now)
        reconciled = reconcile_all_account_scopes_if_due(session, now=now)
        session.commit()
    return processed + reconciled


def reconcile_all_account_scopes_if_due(session: Session, *, now: datetime | None = None) -> int:
    timestamp = now or _now()
    last_at = session.scalar(
        select(func.max(RuntimeMetricSnapshot.captured_at)).where(
            RuntimeMetricSnapshot.metric_name == SCOPE_RECONCILE_METRIC
        )
    )
    if last_at and not _scope_reconcile_due(timestamp, last_at):
        return 0
    tenant_ids = list(
        session.scalars(
            select(Task.tenant_id)
            .where(Task.type == "group_ai_chat", Task.deleted_at.is_(None))
            .distinct()
        )
    )
    repaired = 0
    for tenant_id in tenant_ids:
        result = reconcile_tenant_all_account_scopes(session, int(tenant_id), now=timestamp)
        repaired += result.created_relations
        _record_reconcile_metric(session, int(tenant_id), timestamp, result)
    return repaired


def _record_reconcile_metric(
    session: Session,
    tenant_id: int,
    timestamp: datetime,
    result: ScopeSyncResult,
) -> None:
    session.add(RuntimeMetricSnapshot(
        captured_at=timestamp,
        metric_name=SCOPE_RECONCILE_METRIC,
        dimension_type="tenant",
        dimension_id=str(tenant_id),
        metric_value=result.created_relations,
        tags={
            "task_count": result.task_count,
            "eligible_accounts": result.eligible_accounts,
        },
    ))


def _wall_time(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


def _scope_reconcile_due(timestamp: datetime, last_at: datetime) -> bool:
    current = _wall_time(timestamp)
    previous = _wall_time(last_at)
    return current.date() != previous.date() or current - previous >= SCOPE_RECONCILE_INTERVAL


def sync_account_to_all_tasks(session: Session, account_id: int, *, now: datetime | None = None) -> int:
    account = session.get(TgAccount, account_id)
    if account is None:
        raise ValueError("account not found")
    tasks = _all_account_tasks(session, account.tenant_id)
    eligible = account_id in eligible_account_ids_for_accounts(session, account.tenant_id, [account_id])
    touched = 0
    for task in tasks:
        existing = _relation(session, task.id, account_id)
        if eligible and existing is None:
            _sync_task_relations(session, task, [account_id])
            existing = _relation(session, task.id, account_id)
        if existing is None:
            continue
        _ensure_daily_coverage(session, task, [account_id], now=now, incremental=eligible)
        touched += 1
    return touched


def eligible_account_ids_for_accounts(session: Session, tenant_id: int, account_ids: list[int]) -> list[int]:
    if not account_ids:
        return []
    accounts = session.scalars(
        _eligible_account_stmt(session, tenant_id).where(TgAccount.id.in_(account_ids))
    )
    eligible = {account.id for account in accounts if _session_is_readable(account)}
    return [account_id for account_id in account_ids if account_id in eligible]


def reconcile_tenant_all_account_scopes(
    session: Session,
    tenant_id: int,
    *,
    now: datetime | None = None,
) -> ScopeSyncResult:
    account_ids = eligible_account_ids(session, tenant_id)
    tasks = _all_account_tasks(session, tenant_id)
    created = 0
    for task in tasks:
        created += _sync_task_relations(session, task, account_ids)
        _ensure_daily_coverage(session, task, account_ids, now=now, incremental=True)
    return ScopeSyncResult(task_count=len(tasks), created_relations=created, eligible_accounts=len(account_ids))


def _all_account_tasks(session: Session, tenant_id: int) -> list[Task]:
    tasks = session.scalars(
        select(Task).where(
            Task.tenant_id == tenant_id,
            Task.type == "group_ai_chat",
            Task.deleted_at.is_(None),
            Task.status.in_(("draft", "pending", "running", "paused")),
        )
    )
    selected = [task for task in tasks if is_all_accounts_task(task)]
    for task in selected:
        _normalize_all_account_config(task)
    return selected


def _normalize_all_account_config(task: Task) -> None:
    task.type_config = apply_group_ai_account_coverage_defaults(
        task.type,
        task.type_config or {},
        task.account_config or {},
    )


def _sync_task_relations(session: Session, task: Task, account_ids: list[int]) -> int:
    if not account_ids:
        return 0
    target = _task_target(session, task)
    existing = set(
        session.scalars(
            select(TaskMembershipAdmissionItem.account_id).where(
                TaskMembershipAdmissionItem.task_id == task.id,
                TaskMembershipAdmissionItem.account_id.in_(account_ids),
            )
        )
    )
    missing = [account_id for account_id in account_ids if account_id not in existing]
    session.add_all([
        TaskMembershipAdmissionItem(
            tenant_id=task.tenant_id,
            task_id=task.id,
            account_id=account_id,
            target_id=target.id,
            phase="pending",
        )
        for account_id in missing
    ])
    session.flush()
    return len(missing)


def _task_target(session: Session, task: Task) -> OperationTarget:
    config = task.type_config or {}
    target_id = int(config.get("target_operation_target_id") or 0)
    target = session.get(OperationTarget, target_id) if target_id else None
    if target is None:
        group_id = int(config.get("target_group_id") or 0)
        group = session.get(TgGroup, group_id) if group_id else None
        target = session.scalar(
            select(OperationTarget).where(
                OperationTarget.tenant_id == task.tenant_id,
                OperationTarget.tg_peer_id == group.tg_peer_id,
            )
        ) if group else None
    if target is None and group is not None and group.tenant_id == task.tenant_id:
        target = _create_task_target_from_group(session, task, group)
    if target is None or target.tenant_id != task.tenant_id:
        raise ValueError("all-account coverage task operation target not found")
    return target


def _create_task_target_from_group(session: Session, task: Task, group: TgGroup) -> OperationTarget:
    target = OperationTarget(
        tenant_id=task.tenant_id,
        target_type="group",
        tg_peer_id=group.tg_peer_id,
        title=group.title,
        member_count=group.member_count,
        can_send=group.can_send,
        auth_status=group.auth_status,
    )
    session.add(target)
    session.flush()
    task.type_config = {**(task.type_config or {}), "target_operation_target_id": target.id}
    return target


def _scope_exists(session: Session, task: Task) -> bool:
    return session.scalar(
        select(TaskMembershipAdmissionItem.id)
        .where(TaskMembershipAdmissionItem.task_id == task.id)
        .limit(1)
    ) is not None


def _relation(session: Session, task_id: str, account_id: int) -> TaskMembershipAdmissionItem | None:
    return session.scalar(
        select(TaskMembershipAdmissionItem).where(
            TaskMembershipAdmissionItem.task_id == task_id,
            TaskMembershipAdmissionItem.account_id == account_id,
        )
    )


def _ensure_daily_coverage(
    session: Session,
    task: Task,
    account_ids: list[int],
    *,
    now: datetime | None,
    incremental: bool,
) -> None:
    from .daily_coverage import ensure_task_daily_coverage

    ensure_task_daily_coverage(
        session,
        task,
        now=now,
        account_ids=account_ids,
        incremental=incremental,
    )


def _session_is_readable(account: TgAccount) -> bool:
    try:
        return bool(decrypt_session(account.session_ciphertext))
    except Exception:
        return False


def _rescue_admin_account_id(session: Session, tenant_id: int) -> int:
    tenant = session.get(Tenant, tenant_id)
    return int(tenant.group_rescue_admin_account_id or 0) if tenant else 0


__all__ = [
    "ScopeSyncResult",
    "bootstrap_missing_all_account_task_scope",
    "eligible_account_ids",
    "drain_account_scope_events",
    "emit_account_eligibility_event",
    "initialize_all_account_task_scope",
    "is_all_accounts_task",
    "process_account_eligibility_events",
    "reconcile_all_account_scopes_if_due",
    "scoped_account_ids",
    "reconcile_tenant_all_account_scopes",
    "sync_account_to_all_tasks",
]
