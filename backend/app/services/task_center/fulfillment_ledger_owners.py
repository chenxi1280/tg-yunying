from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, ViewFulfillmentObligation


def resolve_view_task_day_ledger_id(
    session: Session,
    action: Action,
    payload_ledger_id: str,
) -> str | None:
    payload = dict(action.payload or {})
    owner_id = str(payload.get("view_fulfillment_obligation_id") or "")
    if action.action_type != "view_message" or not owner_id:
        return None
    owner = session.get(ViewFulfillmentObligation, owner_id)
    if owner is None or not owner.task_day_ledger_id:
        raise ValueError("fulfillment_view_ledger_missing")
    owner_ledger_id = str(owner.task_day_ledger_id)
    if payload_ledger_id and payload_ledger_id != owner_ledger_id:
        raise ValueError("fulfillment_ledger_identity_conflict")
    return owner_ledger_id


def align_view_ledger_for_safe_settlement(
    session: Session,
    action: Action,
) -> dict[str, str] | None:
    payload = dict(action.payload or {})
    owner_id = str(payload.get("view_fulfillment_obligation_id") or "")
    if action.action_type != "view_message" or not owner_id:
        return None
    owner = session.get(ViewFulfillmentObligation, owner_id)
    if owner is None or not owner.task_day_ledger_id:
        raise ValueError("fulfillment_view_ledger_missing")
    owner_ledger_id = str(owner.task_day_ledger_id)
    payload_ledger_id = str(payload.get("task_day_ledger_id") or "")
    if payload_ledger_id == owner_ledger_id:
        return None
    gateway_started = session.scalar(select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).limit(1))
    if gateway_started:
        raise RuntimeError("view_ledger_alignment_gateway_evidence_exists")
    action.payload = {**payload, "task_day_ledger_id": owner_ledger_id}
    return {"from_ledger_id": payload_ledger_id, "to_ledger_id": owner_ledger_id}
