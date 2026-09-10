"""Project rescue evidence without reopening its execution identity."""
from sqlalchemy import select
from sqlalchemy.orm import object_session

from app.models import Action, ExecutionAttempt, FulfillmentObligationProjection
from .channel_remote_evidence import remote_mutation_state


UNKNOWN_STATUSES = frozenset({"unknown_after_send", "closed_unknown"})
TERMINAL_RESCUE_STATUSES = {
    "success": "invite_success", "failed": "invite_failed", "skipped": "skipped",
    "cancelled": "cancelled", "expired": "expired",
}


def rescue_status(action) -> str:
    if action.status in UNKNOWN_STATUSES:
        return action.status
    state = _obligation_state(action)
    if state == "closed_with_unknown_shortfall":
        return "closed_unknown"
    if action.status in TERMINAL_RESCUE_STATUSES:
        return TERMINAL_RESCUE_STATUSES[action.status]
    if state and state != "open":
        return state
    return "pending"


def rescue_result_snapshot(action) -> dict:
    result = dict(action.result or {})
    if action.action_type in {"invite_group_account", "invite_group_bot"}:
        return {**result, "rescue_status": rescue_status(action)}
    session = object_session(action)
    rescue_id = result.get("group_rescue_action_id")
    if not session or not rescue_id:
        return result
    rescue = session.get(Action, rescue_id)
    if rescue is None or rescue.tenant_id != action.tenant_id or rescue.task_id != action.task_id:
        return result
    return {**result, "group_rescue_status": rescue_status(rescue)}


def refresh_blocked_status(session, action) -> str:
    with session.no_autoflush:
        session.refresh(action, with_for_update=True)
    status = rescue_status(action)
    state = _obligation_state(action)
    if action.status in UNKNOWN_STATUSES or action.status == "success":
        return status
    if state and state != "open":
        return status
    attempts = session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.tenant_id == action.tenant_id,
        ExecutionAttempt.action_id == action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ))
    if any(remote_mutation_state(action, attempt) != "false" for attempt in attempts):
        return "unknown_after_send"
    return ""


def _obligation_state(action) -> str:
    result = dict(action.result or {})
    session = object_session(action)
    if session is None or not action.obligation_id or not action.obligation_type:
        return str(result.get("obligation_state") or "")
    state = session.scalar(select(FulfillmentObligationProjection.state).where(
        FulfillmentObligationProjection.tenant_id == action.tenant_id,
        FulfillmentObligationProjection.task_id == action.task_id,
        FulfillmentObligationProjection.obligation_type == action.obligation_type,
        FulfillmentObligationProjection.obligation_id == action.obligation_id,
    ))
    return str(state or result.get("obligation_state") or "")
