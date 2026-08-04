from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import (
    Action,
    SearchClickAssignment,
    SearchClickFulfillmentObligation,
)


SAFE_NOT_EXECUTED_FACT = "safely_not_executed"
SEARCH_CLICK_ACTION_TYPES = frozenset({"search_join", "search_join_membership"})
REPLAYABLE_ASSIGNMENT_STATES = frozenset({
    "open",
    "action_bound",
    "claiming",
    "executing",
    "gateway_unknown",
    SAFE_NOT_EXECUTED_FACT,
})
TERMINAL_ASSIGNMENT_STATES = frozenset({"confirmed", "closed_unknown"})


def settle_search_click_assignment_from_remote_fact(
    session: Session,
    action: Action,
    fact_kind: str,
) -> bool:
    """Project an authoritative safe-absence fact into the direct search lane."""
    if not _is_direct_search_action(action) or fact_kind != SAFE_NOT_EXECUTED_FACT:
        return False
    assignment, obligation = _bound_rows(session, action)
    _validate_assignment_state(assignment, action)
    changed = _release_assignment(session, assignment)
    if obligation.source_action_id == action.id:
        cleared = session.execute(
            update(SearchClickFulfillmentObligation)
            .where(
                SearchClickFulfillmentObligation.id == obligation.id,
                SearchClickFulfillmentObligation.source_action_id == action.id,
            )
            .values(source_action_id=None, status="open")
        ).rowcount
        if cleared != 1:
            session.expire(obligation)
            if obligation.source_action_id not in (None, ""):
                raise ValueError("search_click_safe_fact_source_action_conflict")
        changed = True
    elif obligation.source_action_id not in (None, ""):
        raise ValueError("search_click_safe_fact_source_action_conflict")
    return changed


def _is_direct_search_action(action: Action) -> bool:
    return (
        action.task_type == "search_click"
        and action.action_type in SEARCH_CLICK_ACTION_TYPES
    )


def _bound_rows(
    session: Session,
    action: Action,
) -> tuple[SearchClickAssignment, SearchClickFulfillmentObligation]:
    payload = dict(action.payload or {})
    assignment_id = str(payload.get("search_click_assignment_id") or "")
    obligation_id = str(payload.get("search_click_obligation_id") or "")
    assignment = session.get(SearchClickAssignment, assignment_id)
    obligation = session.get(SearchClickFulfillmentObligation, obligation_id)
    if assignment is None or obligation is None:
        raise ValueError("search_click_safe_fact_binding_missing")
    if assignment.obligation_id != obligation.id:
        raise ValueError("search_click_safe_fact_obligation_mismatch")
    return assignment, obligation


def _validate_assignment_state(
    assignment: SearchClickAssignment,
    action: Action,
) -> None:
    if assignment.action_id not in ("", action.id):
        raise ValueError("search_click_safe_fact_action_conflict")
    if assignment.state in TERMINAL_ASSIGNMENT_STATES:
        raise ValueError("search_click_safe_fact_terminal_assignment_conflict")
    if assignment.state not in REPLAYABLE_ASSIGNMENT_STATES:
        raise ValueError("search_click_safe_fact_assignment_state_invalid")


def _release_assignment(session: Session, assignment: SearchClickAssignment) -> bool:
    if assignment.state == SAFE_NOT_EXECUTED_FACT:
        return False
    expected_version = int(assignment.version or 1)
    changed = session.execute(
        update(SearchClickAssignment)
        .where(
            SearchClickAssignment.id == assignment.id,
            SearchClickAssignment.version == expected_version,
            SearchClickAssignment.state.in_(
                REPLAYABLE_ASSIGNMENT_STATES - {SAFE_NOT_EXECUTED_FACT}
            ),
        )
        .values(
            state=SAFE_NOT_EXECUTED_FACT,
            version=expected_version + 1,
        )
    ).rowcount
    if changed != 1:
        session.expire(assignment)
        if assignment.state == SAFE_NOT_EXECUTED_FACT:
            return False
        raise ValueError("search_click_safe_fact_assignment_cas_conflict")
    session.expire(assignment)
    return True


__all__ = [
    "SAFE_NOT_EXECUTED_FACT",
    "settle_search_click_assignment_from_remote_fact",
]
