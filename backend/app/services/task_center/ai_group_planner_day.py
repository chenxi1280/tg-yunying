"""Commit the task calendar before independently fallible content planning."""
from sqlalchemy import select

from app.models import TaskDayLedger, TaskGroupDailyTarget

from .account_assignment_eligibility import UNIFIED_CONTRACT
from .daily_ledgers import _day_bounds, ensure_task_day_ledger
from .task_retirement import lock_task_for_planning


def prepare_ai_group_task_day(session_factory, task_id, *, now):
    with session_factory() as session:
        task = lock_task_for_planning(session, task_id)
        if task is None or task.deleted_at is not None or task.type != "group_ai_chat":
            return False
        if (task.type_config or {}).get("engagement_contract_version") != UNIFIED_CONTRACT:
            return False
        local_now, period_start, _deadline = _day_bounds(now, task.timezone)
        if _calendar_materialized(session, task.id, period_start=period_start,
                local_date=local_now.date()):
            return False
        ensure_task_day_ledger(session, task, now=now)
        session.commit()
        return True


def _calendar_materialized(session, task_id, *, period_start, local_date):
    current = session.scalar(select(TaskDayLedger.id).join(
        TaskGroupDailyTarget,
        TaskGroupDailyTarget.task_day_ledger_id == TaskDayLedger.id,
    ).where(
        TaskDayLedger.task_id == task_id,
        TaskDayLedger.period_start_at == period_start,
        TaskGroupDailyTarget.task_id == task_id,
        TaskGroupDailyTarget.target_date == local_date,
    ).limit(1))
    if current is None:
        return False
    expired = session.scalar(select(TaskDayLedger.id).where(
        TaskDayLedger.task_id == task_id,
        TaskDayLedger.deadline_at <= period_start,
        TaskDayLedger.lifecycle_status.in_(("open", "closed_source_ingestion_unproven")),
    ).limit(1))
    return expired is None
