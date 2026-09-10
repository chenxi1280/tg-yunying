"""Supply free search slots through the regular due-task planner."""
from sqlalchemy import and_, func, or_, select

from app.models import Task, TaskPlannerWakeState
from app.services._common import _now
from app.timezone import as_beijing, as_beijing_aware
from .executors.search_click_direct import _free_search_slots


SEARCH_CONTRACT = "fact_first_v3"


def refill_search_lane(session_factory, *, limit, exclude_task_ids, plan_due):
    if limit <= 0:
        return
    with session_factory() as session:
        if _free_search_slots(session) <= 0:
            return
        task_ids = _due_task_ids(session, limit=limit, exclude_task_ids=exclude_task_ids)
    for task_id in task_ids:
        plan_due(task_id, search_refill_eligible)


def search_refill_eligible(session, task):
    if (task.type != "search_click" or task.fulfillment_contract_version != SEARCH_CONTRACT
            or task.status != "running" or task.retired_at is not None or task.deleted_at is not None):
        return False
    wake = session.scalar(select(TaskPlannerWakeState).where(
        TaskPlannerWakeState.tenant_id == task.tenant_id, TaskPlannerWakeState.task_id == task.id))
    due = wake.not_before_at if wake is not None else task.next_run_at
    return (due is None or as_beijing(due) <= as_beijing(_now())) and _free_search_slots(session) > 0


def _due_task_ids(session, *, limit, exclude_task_ids):
    now = as_beijing_aware(_now())
    wake_due = or_(TaskPlannerWakeState.not_before_at.is_(None), TaskPlannerWakeState.not_before_at <= now)
    legacy_due = or_(Task.next_run_at.is_(None), Task.next_run_at <= now)
    query = select(Task.id).outerjoin(TaskPlannerWakeState, and_(
        TaskPlannerWakeState.tenant_id == Task.tenant_id, TaskPlannerWakeState.task_id == Task.id,
    )).where(
        Task.type == "search_click", Task.fulfillment_contract_version == SEARCH_CONTRACT,
        Task.status == "running", Task.retired_at.is_(None), Task.deleted_at.is_(None),
        or_(and_(TaskPlannerWakeState.id.is_not(None), wake_due),
            and_(TaskPlannerWakeState.id.is_(None), legacy_due)),
    )
    if exclude_task_ids:
        query = query.where(Task.id.not_in(exclude_task_ids))
    query = query.order_by(Task.priority.asc(),
        func.coalesce(TaskPlannerWakeState.not_before_at, Task.next_run_at).asc().nullsfirst(),
        Task.created_at.asc()).limit(limit)
    return list(session.scalars(query))
