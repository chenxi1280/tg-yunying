"""Canonical ledger ownership for typed fulfillment facts."""
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Action, Task, TaskGroupDailyMessageSlot, FulfillmentObligationProjection
from .fulfillment_ledger_owners import resolve_view_task_day_ledger_id

def _task_day_ledger_id(
    session: Session,
    action: Action,
    *,
    require_current_ai_send: bool = False,
) -> str | None:
    payload_ledger = str(dict(action.payload or {}).get("task_day_ledger_id") or "")
    view_ledger = resolve_view_task_day_ledger_id(session, action, payload_ledger)
    if view_ledger:
        return view_ledger
    quantity_id = str(action.primary_quantity_slot_id or "")
    if not quantity_id:
        task = session.get(Task, action.task_id)
        current_ai_send = bool(
            task
            and task.fulfillment_contract_version == "fact_first_v3"
            and action.task_type == "group_ai_chat"
            and action.action_type == "send_message"
        )
        if require_current_ai_send and current_ai_send and not payload_ledger:
            raise ValueError("fulfillment_ai_ledger_missing")
        return payload_ledger or None
    quantity = session.get(TaskGroupDailyMessageSlot, quantity_id)
    if quantity is None or not quantity.task_day_ledger_id:
        raise ValueError("fulfillment_quantity_ledger_missing")
    owner_ledger = str(quantity.task_day_ledger_id)
    if payload_ledger and payload_ledger != owner_ledger:
        raise ValueError("fulfillment_ledger_identity_conflict")
    return payload_ledger or owner_ledger


def _bind_projection_ledger(
    projection: FulfillmentObligationProjection,
    ledger_id: str | None,
) -> None:
    if not ledger_id:
        return
    current = str(projection.task_day_ledger_id or "")
    if current and current != ledger_id:
        raise ValueError("fulfillment_projection_ledger_conflict")
    projection.task_day_ledger_id = ledger_id


def _bind_existing_projection_ledger(
    session: Session,
    obligation_type: str,
    obligation_id: str,
    *,
    ledger_id: str | None,
) -> None:
    if not ledger_id:
        return
    projection = session.scalar(select(FulfillmentObligationProjection).where(
        FulfillmentObligationProjection.obligation_type == obligation_type,
        FulfillmentObligationProjection.obligation_id == obligation_id,
    ))
    if projection is not None:
        _bind_projection_ledger(projection, ledger_id)


