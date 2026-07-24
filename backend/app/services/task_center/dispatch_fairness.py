"""Persistent claim-class fairness between hard-hourly and ordinary actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import DispatchFairnessCursor
from app.services._common import _now


ClaimClass = Literal[
    "target_admission_retry",
    "search_join_membership",
    "search_join",
    "hard_hourly",
    "channel_comment",
    "ordinary",
]

@dataclass(frozen=True)
class FairnessDecision:
    preferred_class: ClaimClass | None
    reason: str


def record_claim_class(
    session: Session,
    *,
    tenant_id: int,
    claimed_class: ClaimClass,
    reason: str = "selected",
) -> None:
    """Record a tenant's persisted claim-category selection under a row lock."""
    cursor = _cursor_for_update(session, tenant_id)
    _record_cursor(cursor, claimed_class, reason)


def reserve_fairness_decision(
    session: Session,
    *,
    tenant_id: int,
    has_due_ordinary: bool,
    has_due_hard_hourly: bool,
    has_due_higher_priority: bool = False,
) -> FairnessDecision:
    """Lock, decide, and persist one tenant's next claim class atomically."""
    unavailable = _unavailable_decision(
        has_due_ordinary,
        has_due_hard_hourly,
        has_due_higher_priority,
    )
    if unavailable is not None:
        return unavailable
    cursor = _cursor_for_update(session, tenant_id)
    decision = _decision_from_last_claim(cursor.last_claim_class)
    claimed_class = "ordinary" if decision.preferred_class == "ordinary" else "hard_hourly"
    _record_cursor(cursor, claimed_class, decision.reason)
    return decision


def _record_cursor(cursor: DispatchFairnessCursor, claimed_class: ClaimClass, reason: str) -> None:
    cursor.last_claim_class = claimed_class
    cursor.last_reason = reason
    cursor.updated_at = _now()


def should_prefer_ordinary_after_hard_hourly(
    session: Session,
    *,
    tenant_id: int,
    has_due_ordinary: bool,
    has_due_hard_hourly: bool,
    has_due_higher_priority: bool = False,
) -> FairnessDecision:
    """Read the current cursor before the caller persists its short selection transaction."""
    unavailable = _unavailable_decision(
        has_due_ordinary,
        has_due_hard_hourly,
        has_due_higher_priority,
    )
    if unavailable is not None:
        return unavailable
    cursor = session.scalar(select(DispatchFairnessCursor).where(DispatchFairnessCursor.tenant_id == tenant_id))
    return _decision_from_last_claim(cursor.last_claim_class if cursor else "")


def _unavailable_decision(
    has_due_ordinary: bool,
    has_due_hard_hourly: bool,
    has_due_higher_priority: bool,
) -> FairnessDecision | None:
    if not has_due_ordinary:
        return FairnessDecision(None, "no_due_ordinary")
    if not has_due_hard_hourly:
        return FairnessDecision(None, "no_due_hard_hourly")
    if has_due_higher_priority:
        return FairnessDecision(None, "higher_priority_due")
    return None


def _decision_from_last_claim(last_claim_class: str) -> FairnessDecision:
    if last_claim_class == "hard_hourly":
        return FairnessDecision("ordinary", "hard_hourly_then_ordinary")
    return FairnessDecision(None, "hard_hourly_allowed")


def _cursor_for_update(session: Session, tenant_id: int) -> DispatchFairnessCursor:
    statement = select(DispatchFairnessCursor).where(DispatchFairnessCursor.tenant_id == tenant_id)
    if session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update()
    cursor = session.scalar(statement)
    if cursor:
        return cursor
    return _create_cursor(session, tenant_id)


def _create_cursor(session: Session, tenant_id: int) -> DispatchFairnessCursor:
    cursor = DispatchFairnessCursor(tenant_id=tenant_id)
    try:
        with session.begin_nested():
            session.add(cursor)
            session.flush()
        return cursor
    except IntegrityError:
        statement = select(DispatchFairnessCursor).where(DispatchFairnessCursor.tenant_id == tenant_id)
        if session.bind and session.bind.dialect.name != "sqlite":
            statement = statement.with_for_update()
        existing = session.scalar(statement)
        if existing:
            return existing
    raise RuntimeError(f"无法建立租户 {tenant_id} 的调度公平游标")


def classify_action_payload(action_type: str, payload: dict | None, task_type: str | None = None) -> ClaimClass:
    payload = payload or {}
    if action_type == "target_admission_retry":
        return "target_admission_retry"
    if action_type in {"ensure_target_membership", "ensure_channel_membership"}:
        if task_type == "search_join_group" or payload.get("search_join_membership"):
            return "search_join_membership"
        return "ordinary"
    if action_type == "search_join":
        return "search_join"
    if action_type == "send_message" and payload.get("hard_hourly_target"):
        return "hard_hourly"
    if action_type in {"post_comment", "channel_comment"} or task_type == "channel_comment":
        return "channel_comment"
    return "ordinary"


__all__ = [
    "ClaimClass",
    "FairnessDecision",
    "classify_action_payload",
    "record_claim_class",
    "reserve_fairness_decision",
    "should_prefer_ordinary_after_hard_hourly",
]
