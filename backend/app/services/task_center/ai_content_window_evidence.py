"""Positive nonexecution evidence for retiring a terminal content owner."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, FulfillmentRemoteFact


TERMINAL_FAILURE_ATTEMPT_STATES = frozenset({"failed", "skipped", "cancelled", "permanent_failed"})
SAFE_NONEXECUTION_FACT = "safely_not_executed"


def content_action_proven_unsent(session: Session, action: Action) -> bool:
    attempts = list(session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id == action.id).with_for_update()))
    facts = list(session.scalars(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.action_id == action.id).with_for_update()))
    if not attempts:
        return not facts
    attempt_ids = frozenset(attempt.id for attempt in attempts)
    if any(not _fact_matches_owner(fact, action) or fact.attempt_id not in attempt_ids for fact in facts):
        return False
    proven_ids = frozenset(fact.attempt_id for fact in facts)
    return all(_attempt_proven_unsent(attempt, action, proven_ids=proven_ids) for attempt in attempts)


def _fact_matches_owner(fact: FulfillmentRemoteFact, action: Action) -> bool:
    return (
        fact.fact_kind == SAFE_NONEXECUTION_FACT
        and fact.tenant_id == action.tenant_id
        and fact.task_id == action.task_id
        and fact.task_type == action.task_type
        and fact.obligation_type == action.obligation_type
        and fact.obligation_id == action.obligation_id
        and fact.mutation_kind == action.action_type
    )


def _attempt_proven_unsent(attempt: ExecutionAttempt, action: Action, *, proven_ids: frozenset[str]) -> bool:
    if (
        attempt.tenant_id != action.tenant_id
        or attempt.task_lifecycle_epoch != action.task_lifecycle_epoch
        or attempt.after_call_at is None
        or attempt.remote_message_id
    ):
        return False
    if attempt.status == "skipped_before_gateway" and attempt.gateway_call_started_at is None:
        return True
    return attempt.status in TERMINAL_FAILURE_ATTEMPT_STATES and attempt.id in proven_ids
