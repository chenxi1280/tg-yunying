from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    DispatchClaimReservation,
    ExecutionAttempt,
    SearchClickOpportunityAssignment,
    TaskDayLedger,
    ViewFulfillmentObligation,
)


SAMPLE_LIMIT = 8
OPEN_ACTION_STATUSES = ("pending", "claiming", "executing")


def ai_open_action_details(
    session: Session,
    ledger: TaskDayLedger,
    actions: list[Action],
) -> dict[str, Any]:
    ordered = sorted(actions, key=_action_schedule_key)[:SAMPLE_LIMIT]
    error_counts = Counter(_action_error_code(action) for action in actions)
    stage_counts = Counter(_action_generation_stage(action) for action in actions)
    return {
        "error_code_counts": _nonempty_counts(error_counts),
        "generation_stage_counts": _nonempty_counts(stage_counts),
        "oldest_open_action_samples": [
            _ai_action_row(ledger, action) for action in ordered
        ],
    }


def search_claimed_details(
    session: Session,
    ledger: TaskDayLedger,
) -> dict[str, Any]:
    assignments = list(
        session.scalars(
            select(SearchClickOpportunityAssignment)
            .where(
                SearchClickOpportunityAssignment.task_day_ledger_id == ledger.id,
                SearchClickOpportunityAssignment.state == "claimed",
            )
            .order_by(
                SearchClickOpportunityAssignment.updated_at.asc(),
                SearchClickOpportunityAssignment.id.asc(),
            )
        )
    )
    actions = _action_map(session, {row.action_id for row in assignments if row.action_id})
    action_counts = Counter(_linked_action_status(actions, row.action_id) for row in assignments)
    return {
        "claimed_action_status_counts": _nonempty_counts(action_counts),
        "oldest_claimed_samples": [
            _search_assignment_row(session, row, actions) for row in assignments[:SAMPLE_LIMIT]
        ],
    }


def view_open_details(
    session: Session,
    ledger: TaskDayLedger,
) -> dict[str, Any]:
    obligations = list(
        session.scalars(
            select(ViewFulfillmentObligation)
            .where(
                ViewFulfillmentObligation.task_day_ledger_id == ledger.id,
                ViewFulfillmentObligation.status != "confirmed",
            )
            .order_by(
                ViewFulfillmentObligation.created_at.asc(),
                ViewFulfillmentObligation.id.asc(),
            )
        )
    )
    actions = _action_map(
        session,
        {row.current_action_id for row in obligations if row.current_action_id},
    )
    action_counts = Counter(
        _linked_action_status(actions, row.current_action_id) for row in obligations
    )
    return {
        "open_action_status_counts": _nonempty_counts(action_counts),
        "oldest_open_obligation_samples": [
            _view_obligation_row(row, actions) for row in obligations[:SAMPLE_LIMIT]
        ],
    }


def _ai_action_row(ledger: TaskDayLedger, action: Action) -> dict[str, Any]:
    payload = dict(action.payload or {})
    result = dict(action.result or {})
    return {
        "id": action.id,
        "account_id": action.account_id,
        "status": action.status,
        "scheduled_at": _iso(action.scheduled_at),
        "created_at": _iso(action.created_at),
        "age_seconds": _age_seconds(action.created_at),
        "generation_status": payload.get("ai_generation_status"),
        "generation_stage": result.get("generation_stage"),
        "error_code": result.get("error_code"),
        "message_text_present": bool(str(payload.get("message_text") or "").strip()),
        "context_snapshot_message_id": payload.get("context_snapshot_message_id"),
        "latest_context_message_id": result.get("latest_context_message_id"),
        "speaker_rotation_reason": result.get("speaker_rotation_reason"),
        "ledger_matches": str(payload.get("task_day_ledger_id") or "") == ledger.id,
    }


def _search_assignment_row(
    session: Session,
    assignment: SearchClickOpportunityAssignment,
    actions: dict[str, Action],
) -> dict[str, Any]:
    action = actions.get(str(assignment.action_id or ""))
    reservation = session.get(
        DispatchClaimReservation,
        assignment.dispatch_claim_reservation_id,
    )
    attempt = _latest_attempt(session, action.id) if action is not None else None
    return {
        "assignment_id": assignment.id,
        "action_id": assignment.action_id,
        "account_id": assignment.account_id,
        "updated_at": _iso(assignment.updated_at),
        "age_seconds": _age_seconds(assignment.updated_at),
        "action_status": action.status if action else "missing",
        "action_scheduled_at": _iso(action.scheduled_at) if action else None,
        "action_error_code": _action_error_code(action) if action else "",
        "attempt_status": attempt.status if attempt else "missing",
        "gateway_call_started_at": _iso(attempt.gateway_call_started_at) if attempt else None,
        "reservation_reason": reservation.reason if reservation else "missing",
        "reservation_claimed_count": reservation.claimed_count if reservation else None,
        "reservation_bound_count": reservation.bound_count if reservation else None,
    }


def _view_obligation_row(
    obligation: ViewFulfillmentObligation,
    actions: dict[str, Action],
) -> dict[str, Any]:
    action = actions.get(str(obligation.current_action_id or ""))
    return {
        "obligation_id": obligation.id,
        "channel_message_id": obligation.channel_message_id,
        "account_id": obligation.account_id,
        "status": obligation.status,
        "current_action_id": obligation.current_action_id,
        "created_at": _iso(obligation.created_at),
        "age_seconds": _age_seconds(obligation.created_at),
        "action_status": action.status if action else "missing",
        "action_scheduled_at": _iso(action.scheduled_at) if action else None,
        "action_error_code": _action_error_code(action) if action else "",
    }


def _latest_attempt(session: Session, action_id: str) -> ExecutionAttempt | None:
    return session.scalar(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.action_id == action_id)
        .order_by(ExecutionAttempt.attempt_no.desc(), ExecutionAttempt.created_at.desc())
        .limit(1)
    )


def _linked_action_status(actions: dict[str, Action], action_id: str | None) -> str:
    action = actions.get(str(action_id or ""))
    return str(action.status if action else "missing")


def _action_map(session: Session, action_ids: set[str]) -> dict[str, Action]:
    if not action_ids:
        return {}
    rows = session.scalars(select(Action).where(Action.id.in_(action_ids)))
    return {str(row.id): row for row in rows}


def _action_generation_stage(action: Action) -> str:
    return str((action.result or {}).get("generation_stage") or "")


def _action_error_code(action: Action) -> str:
    return str((action.result or {}).get("error_code") or "")


def _action_schedule_key(action: Action) -> tuple[datetime, str]:
    value = action.scheduled_at or action.created_at or datetime.max.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value, str(action.id)


def _nonempty_counts(counts: Counter[str]) -> dict[str, int]:
    return dict(sorted((key or "none", int(value)) for key, value in counts.items()))


def _age_seconds(value: datetime | None) -> int | None:
    if value is None:
        return None
    normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return max(int((datetime.now(timezone.utc) - normalized).total_seconds()), 0)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


__all__ = ["ai_open_action_details", "search_claimed_details", "view_open_details"]
