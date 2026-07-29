from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Task,
    TaskAccountDailyCoverage,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TaskGroupDailyTarget,
    TaskMembershipAdmissionItem,
    TaskDayLedgerLifecycleEvent,
    SearchClickFulfillmentObligation,
    OperationTarget,
    TgGroup,
)
from app.services._common import _now

from .daily_coverage import ensure_task_daily_coverage
from .daily_group_target import ensure_task_group_daily_target
from .search_click_revisions import apply_pending_search_click_revision
from .search_click_target_progress import _active_click_quarantine_count


def ensure_task_day_ledger(
    session: Session,
    task: Task,
    *,
    now: datetime | None = None,
) -> TaskDayLedger:
    timestamp = now or _now()
    local_now, period_start, deadline = _day_bounds(timestamp, task.timezone)
    ledger = _find_ledger(session, task.id, period_start)
    if ledger is None:
        _close_expired_ledgers(session, task, period_start)
        if task.type == "search_click":
            apply_pending_search_click_revision(task, period_start=period_start)
        ledger = _new_ledger(task, local_now, period_start, deadline)
        session.add(ledger)
        session.flush()
    if task.type == "group_ai_chat":
        _materialize_group_slots(session, task, ledger, local_now)
    if task.type == "search_click":
        _materialize_search_click_obligations(session, task, ledger)
    return ledger


def _close_expired_ledgers(
    session: Session,
    task: Task,
    period_start: datetime,
) -> None:
    ledgers = session.scalars(
        select(TaskDayLedger).where(
            TaskDayLedger.task_id == task.id,
            TaskDayLedger.lifecycle_status == "open",
            TaskDayLedger.deadline_at <= period_start,
        )
    )
    for ledger in ledgers:
        outcome = _ledger_deadline_outcome(session, task, ledger)
        ledger.lifecycle_status = f"closed_{outcome}"
        session.add(TaskDayLedgerLifecycleEvent(
            tenant_id=task.tenant_id,
            task_day_ledger_id=ledger.id,
            event_type=f"deadline_{outcome}",
            occurred_at=ledger.deadline_at,
            task_revision=task.config_revision,
        ))


def _ledger_deadline_outcome(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
) -> str:
    if task.type != "search_click":
        return "closed"
    obligations = list(session.scalars(
        select(SearchClickFulfillmentObligation).where(
            SearchClickFulfillmentObligation.task_day_ledger_id == ledger.id
        )
    ))
    fully_confirmed = bool(obligations) and all(
        row.status == "confirmed"
        and row.target_click_observed
        and bool(row.click_evidence_hash)
        for row in obligations
    )
    quarantined = _active_click_quarantine_count(session, obligations) > 0
    return "met" if fully_confirmed and not quarantined else "missed"


def _day_bounds(
    timestamp: datetime,
    timezone_name: str,
) -> tuple[datetime, datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    aware = timestamp.replace(tzinfo=zone) if timestamp.tzinfo is None else timestamp.astimezone(zone)
    local_start = aware.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return aware, local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _find_ledger(
    session: Session,
    task_id: str,
    period_start: datetime,
) -> TaskDayLedger | None:
    return session.scalar(
        select(TaskDayLedger).where(
            TaskDayLedger.task_id == task_id,
            TaskDayLedger.period_start_at == period_start,
        )
    )


def _new_ledger(
    task: Task,
    local_now: datetime,
    period_start: datetime,
    deadline: datetime,
) -> TaskDayLedger:
    return TaskDayLedger(
        tenant_id=task.tenant_id,
        task_id=task.id,
        timezone_snapshot=task.timezone,
        timezone_revision=task.config_revision,
        obligation_local_date=local_now.date(),
        period_start_at=period_start,
        deadline_at=deadline,
        day_phase=_day_phase(task, local_now),
        planning_anchor_at=local_now.astimezone(timezone.utc),
    )


def _day_phase(task: Task, local_now: datetime) -> str:
    started_at = task.scheduled_start or task.created_at
    if started_at is None:
        return "full_day"
    zone = local_now.tzinfo
    aware = started_at.replace(tzinfo=zone) if started_at.tzinfo is None else started_at.astimezone(zone)
    started_midday = (
        aware.date() == local_now.date()
        and aware.time() > datetime.min.time()
    )
    return "partial_start" if started_midday else "full_day"


def _materialize_group_slots(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    local_now: datetime,
) -> None:
    if _slots_exist(session, ledger.id):
        return
    group = _task_group(session, task)
    items = _scope_items(session, task)
    ensure_task_daily_coverage(
        session,
        task,
        now=local_now.replace(tzinfo=None),
        account_ids=[item.account_id for item in items],
        target_group=group,
    )
    target = ensure_task_group_daily_target(
        session,
        task,
        group,
        ledger.obligation_local_date,
        now=local_now.replace(tzinfo=None),
    )
    target.task_day_ledger_id = ledger.id
    rows = _coverage_rows(session, task.id, ledger.obligation_local_date)
    _bind_coverage_rows(rows, ledger.id)
    _add_slots(session, task, ledger, group, target, items, rows)


def _slots_exist(session: Session, ledger_id: str) -> bool:
    return session.scalar(
        select(TaskGroupDailyMessageSlot.id)
        .where(TaskGroupDailyMessageSlot.task_day_ledger_id == ledger_id)
        .limit(1)
    ) is not None


def _materialize_search_click_obligations(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
) -> None:
    if session.scalar(
        select(SearchClickFulfillmentObligation.id)
        .where(SearchClickFulfillmentObligation.task_day_ledger_id == ledger.id)
        .limit(1)
    ):
        return
    config = task.type_config or {}
    target_id = int(config.get("target_operation_target_id") or 0)
    target_count = int(config.get("daily_click_target_count") or 0)
    if target_id <= 0 or target_count <= 0:
        raise ValueError("search_click_runtime_contract_invalid")
    for ordinal in range(1, target_count + 1):
        session.add(SearchClickFulfillmentObligation(
            tenant_id=task.tenant_id,
            task_day_ledger_id=ledger.id,
            target_id=target_id,
            click_obligation_ordinal=ordinal,
        ))
    session.flush()


def _task_group(session: Session, task: Task) -> TgGroup:
    group_id = int((task.type_config or {}).get("target_group_id") or 0)
    group = session.get(TgGroup, group_id) if group_id else None
    if group is None or group.tenant_id != task.tenant_id:
        raise ValueError("group_ai_chat target group not found")
    return group


def _scope_items(
    session: Session,
    task: Task,
) -> list[TaskMembershipAdmissionItem]:
    return list(session.scalars(
        select(TaskMembershipAdmissionItem)
        .where(TaskMembershipAdmissionItem.task_id == task.id)
        .order_by(TaskMembershipAdmissionItem.account_id.asc())
    ))


def _coverage_rows(
    session: Session,
    task_id: str,
    local_date,
) -> dict[int, TaskAccountDailyCoverage]:
    rows = session.scalars(
        select(TaskAccountDailyCoverage).where(
            TaskAccountDailyCoverage.task_id == task_id,
            TaskAccountDailyCoverage.coverage_date == local_date,
        )
    )
    return {row.account_id: row for row in rows}


def _bind_coverage_rows(
    rows: dict[int, TaskAccountDailyCoverage],
    ledger_id: str,
) -> None:
    for row in rows.values():
        row.task_day_ledger_id = ledger_id


def _add_slots(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    group: TgGroup,
    target: TaskGroupDailyTarget,
    items: list[TaskMembershipAdmissionItem],
    rows: dict[int, TaskAccountDailyCoverage],
) -> None:
    target_id = _target_id(session, task, group, items)
    for ordinal in range(1, target.effective_message_target + 1):
        coverage = rows.get(items[ordinal - 1].account_id) if ordinal <= len(items) else None
        session.add(TaskGroupDailyMessageSlot(
            tenant_id=task.tenant_id,
            task_id=task.id,
            task_day_ledger_id=ledger.id,
            target_operation_target_id=target_id,
            task_account_daily_coverage_id=coverage.id if coverage else None,
            slot_kind="account_coverage" if coverage else "extra_volume",
            slot_ordinal=ordinal,
        ))
    session.flush()


def _target_id(
    session: Session,
    task: Task,
    group: TgGroup,
    items: list[TaskMembershipAdmissionItem],
) -> int:
    configured = int((task.type_config or {}).get("target_operation_target_id") or 0)
    if configured:
        return configured
    if items:
        return int(items[0].target_id)
    target = session.scalar(
        select(OperationTarget).where(
            OperationTarget.tenant_id == task.tenant_id,
            OperationTarget.tg_peer_id == group.tg_peer_id,
        )
    )
    if target:
        return int(target.id)
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
    task.type_config = {
        **(task.type_config or {}),
        "target_operation_target_id": target.id,
    }
    return int(target.id)


__all__ = ["ensure_task_day_ledger"]
