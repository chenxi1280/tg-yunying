"""Prepare pending content successors independently of provider availability."""
from datetime import timezone

from sqlalchemy import select, tuple_

from app.models import Action, GenerationJob, Task, TaskDayLedger, TaskGroupDailyMessageSlot
from app.services._common import _now
from app.timezone import as_beijing

from .ai_group_emergency import select_emergency_content
from .ai_group_emergency_contract import PENDING_STATUS, UNKNOWN_STATUS


def drain_emergency_content(session_factory, limit: int) -> int:
    count = 0
    cursor = None
    while count < limit:
        with session_factory() as session:
            statement = _candidate_statement(limit - count)
            if cursor:
                statement = statement.where(tuple_(Action.created_at, Action.id) > cursor)
            rows = list(session.execute(statement))
        if not rows:
            break
        for action_id, task_id, created_at in rows:
            cursor = (created_at, action_id)
            count += _select_one(session_factory, action_id, task_id)
    return count


def _select_one(session_factory, action_id, task_id) -> int:
    with session_factory() as session:
        task = session.scalar(select(Task).where(Task.id == task_id).with_for_update(skip_locked=True))
        if task is None:
            return 0
        action = session.scalar(select(Action).where(Action.id == action_id).with_for_update(skip_locked=True))
        if action is None:
            return 0
        changed = int(select_emergency_content(session, task, action))
        session.commit()
        return changed


def _candidate_statement(limit):
    now = as_beijing(_now()).astimezone(timezone.utc)
    return (select(Action.id, Action.task_id, Action.created_at)
        .join(Task, Task.id == Action.task_id)
        .join(TaskGroupDailyMessageSlot, TaskGroupDailyMessageSlot.id == Action.primary_quantity_slot_id)
        .join(TaskDayLedger, TaskDayLedger.id == TaskGroupDailyMessageSlot.task_day_ledger_id)
        .join(GenerationJob, GenerationJob.id == Action.payload["generation_job_id"].as_string())
        .where(Action.task_type == "group_ai_chat", Action.action_type == "send_message",
            Action.status == "pending", Action.claim_owner == "", Action.lease_owner == "",
            Action.task_lifecycle_epoch == Task.task_lifecycle_epoch,
            Action.payload["ai_generation_status"].as_string().in_((PENDING_STATUS, UNKNOWN_STATUS)),
            Task.status == "running", Task.deleted_at.is_(None),
            Task.type_config["engagement_contract_version"].as_string() == "unified_engagement_v1",
            Task.type_config["emergency_fallback_enabled"].as_boolean().is_not(False),
            TaskDayLedger.lifecycle_status == "open",
            TaskDayLedger.period_start_at <= now, TaskDayLedger.deadline_at > now,
            TaskGroupDailyMessageSlot.state != "terminal", GenerationJob.state.in_(("unknown", "failed")),
            GenerationJob.generation_owner_id == "", GenerationJob.lease_expires_at.is_(None))
        .order_by(Action.created_at, Action.id).limit(limit))
