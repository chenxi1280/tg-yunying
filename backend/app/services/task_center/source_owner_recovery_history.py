"""Read concrete frozen points while the caller holds the source advisory lock."""
from sqlalchemy import select

from app.models import ReactionFulfillmentObligation, TaskDayLedger, ViewFulfillmentObligation
from .source_pacing import wall_datetime

RECOVERABLE_OWNER_MODELS = (ReactionFulfillmentObligation, ViewFulfillmentObligation)


def frozen_owner_recovery_history(session, task, slots, *, owner_model):
    if owner_model not in RECOVERABLE_OWNER_MODELS:
        return frozenset(), ()
    ids = [slot.owner_id for slot in slots if slot.frozen_due_at is not None
           and slot.release_not_before_at is not None]
    if not ids:
        return frozenset(), ()
    eligible = frozenset(session.scalars(select(owner_model.id).where(
        owner_model.id.in_(ids), owner_model.status == 'open',
        owner_model.current_action_id.is_(None))))
    if not eligible:
        return eligible, ()
    first = slots[0]
    query = select(owner_model.release_not_before_at, owner_model.pacing_due_at,
                   owner_model.pacing_plan_total).where(
        owner_model.tenant_id == task.tenant_id,
        owner_model.task_lifecycle_epoch == first.task_lifecycle_epoch,
        owner_model.pacing_period_key == first.pacing_period_key,
        owner_model.pacing_source_key_hash == first.pacing_source_key_hash,
        owner_model.id.not_in([slot.owner_id for slot in slots]),
        owner_model.release_not_before_at.is_not(None), owner_model.pacing_due_at.is_not(None))
    if owner_model is ViewFulfillmentObligation:
        query = query.join(TaskDayLedger, TaskDayLedger.id == owner_model.task_day_ledger_id).where(
            TaskDayLedger.task_id == task.id)
    else:
        query = query.where(owner_model.task_id == task.id)
    history = tuple((max(wall_datetime(release), wall_datetime(due)), int(total or 0))
                    for release, due, total in session.execute(query))
    return eligible, history
