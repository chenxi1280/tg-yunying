from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ExecutionAttempt,
    SearchClickAssignment,
    SearchClickFulfillmentObligation,
)


def project_search_click_unknown(session: Session, action: Action) -> bool:
    if action.task_type != "search_click" or action.action_type != "search_join":
        return False
    if action.status != "unknown_after_send":
        return False
    payload = dict(action.payload or {})
    assignment_id = str(payload.get("search_click_assignment_id") or "")
    obligation_id = str(payload.get("search_click_obligation_id") or "")
    if not assignment_id or not obligation_id:
        return False
    assignment = session.get(SearchClickAssignment, assignment_id)
    obligation = session.get(SearchClickFulfillmentObligation, obligation_id)
    attempt = session.scalar(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id == action.id,
    ).order_by(ExecutionAttempt.attempt_no.desc()).limit(1))
    _validate_unknown_binding(action, assignment, obligation, attempt)
    changed = assignment.state != "gateway_unknown"
    if changed:
        assignment.state = "gateway_unknown"
        assignment.version = int(assignment.version or 1) + 1
    obligation.status = "unknown_after_send"
    obligation.execution_attempt_id = attempt.id
    return changed


def _validate_unknown_binding(action, assignment, obligation, attempt) -> None:
    if assignment is None or obligation is None:
        raise ValueError("search_click_unknown_binding_missing")
    if attempt is None or attempt.gateway_call_started_at is None:
        raise ValueError("search_click_unknown_gateway_attempt_missing")
    if assignment.action_id != action.id or obligation.source_action_id != action.id:
        raise ValueError("search_click_unknown_action_binding_mismatch")
    if assignment.obligation_id != obligation.id:
        raise ValueError("search_click_unknown_obligation_binding_mismatch")
    if assignment.state not in {"executing", "gateway_unknown"}:
        raise ValueError("search_click_unknown_assignment_state_invalid")
    if obligation.status not in {"action_bound", "unknown_after_send"}:
        raise ValueError("search_click_unknown_obligation_state_invalid")


__all__ = ["project_search_click_unknown"]
