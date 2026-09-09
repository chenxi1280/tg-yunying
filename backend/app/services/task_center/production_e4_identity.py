"""Resolve the real AI Action-to-ledger identity for read-only diagnostics."""
from sqlalchemy import and_, or_, select

from app.models import Action, Task, TaskGroupDailyMessageSlot


UNIFIED_CONTRACT = "unified_engagement_v1"


def action_ledger_scope(session, ledger):
    slot = TaskGroupDailyMessageSlot
    slot_matches = select(slot.id).where(
        slot.id == Action.primary_quantity_slot_id,
        slot.tenant_id == ledger.tenant_id,
        slot.task_id == ledger.task_id,
        slot.task_day_ledger_id == ledger.id,
    ).correlate(Action).exists()
    payload_ledger = Action.payload["task_day_ledger_id"].as_string()
    canonical = and_(slot_matches, or_(payload_ledger.is_(None), payload_ledger == ledger.id))
    task = session.get(Task, ledger.task_id)
    if task is None or (task.type_config or {}).get("engagement_contract_version") == UNIFIED_CONTRACT:
        return canonical
    return or_(canonical, and_(
        Action.primary_quantity_slot_id.is_(None), payload_ledger == ledger.id,
    ))
