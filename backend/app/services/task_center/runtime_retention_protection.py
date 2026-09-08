"""Protect success-query dependencies and business references from detail TTL."""
from datetime import timedelta

from sqlalchemy import or_, select, func

from app.database import Base
from app.models import (
    Action, ExecutionAttempt, FulfillmentFactProjectionState,
    FulfillmentObligationProjection, FulfillmentRemoteFact,
)
from app.timezone import as_beijing_aware

from .runtime_retention_policy import PROTECTED_ATTEMPT_STATUSES
from .success_fact_query import WINDOW_HOURS, valid_success_predicates


# These are the references explicitly released by the existing detail-cleanup contract.
DISPOSABLE_REFERENCE_COLUMNS = frozenset({
    "execution_attempts.action_id", "review_queue.action_id",
    "task_account_daily_coverage.reserved_action_id",
    "task_account_daily_coverage.last_success_action_id",
    "task_membership_admission_items.membership_action_id",
    "task_membership_admission_items.test_message_action_id",
    "task_membership_admission_items.delete_action_id",
    "task_membership_admission_items.rescue_action_id",
    "ai_coverage_variation_intents.action_id",
    "search_rank_deboost_click_reservations.action_id",
    "task_hard_hourly_delivery_credits.action_id",
})


def protected_dependencies(as_of) -> tuple:
    projection = FulfillmentObligationProjection
    base = (
        ("recent_success", _recent_success_dependency(as_of)),
        ("unsettled_attempt", select(ExecutionAttempt.id).where(
            ExecutionAttempt.action_id == Action.id,
            ExecutionAttempt.status.in_(PROTECTED_ATTEMPT_STATUSES),
        ).correlate(Action).exists()),
        ("active_obligation_owner", select(projection.id).where(
            projection.active_action_id == Action.id,
            projection.state.in_(("open", "remote_reconcile_only")),
        ).correlate(Action).exists()),
        ("pending_fact_projection", _pending_fact_projection()),
        ("remote_outcome_unknown", select(FulfillmentRemoteFact.fact_id).where(
            FulfillmentRemoteFact.action_id == Action.id,
            FulfillmentRemoteFact.tenant_id == Action.tenant_id,
            FulfillmentRemoteFact.fact_kind == "remote_outcome_unknown",
        ).correlate(Action).exists()),
    )
    return base + tuple((foreign_key.parent.table.name + "." + foreign_key.parent.name,
                         _reference_dependency(foreign_key))
                        for foreign_key in _business_foreign_keys())


def _recent_success_dependency(as_of):
    fact = FulfillmentRemoteFact
    end = as_beijing_aware(as_of)
    first = select(func.min(fact.observed_at)).select_from(fact).join(
        ExecutionAttempt, ExecutionAttempt.id == fact.attempt_id,
    ).where(*valid_success_predicates()).correlate(Action).scalar_subquery()
    return first.between(end - timedelta(hours=WINDOW_HOURS), end)


def _pending_fact_projection():
    fact, state = FulfillmentRemoteFact, FulfillmentFactProjectionState
    return select(state.id).join(fact, state.fact_id == fact.fact_id).where(
        fact.action_id == Action.id, fact.tenant_id == Action.tenant_id,
        state.state.in_(("pending", "failed")),
    ).correlate(Action).exists()


def _business_foreign_keys() -> tuple:
    references = (foreign_key for table in Base.metadata.tables.values()
                  for foreign_key in table.foreign_keys)
    return tuple(sorted((fk for fk in references
        if fk.column.table.name in {"actions", "execution_attempts"}
        and fk.parent.table.name + "." + fk.parent.name not in DISPOSABLE_REFERENCE_COLUMNS),
        key=lambda fk: (fk.parent.table.name, fk.parent.name)))


def _reference_dependency(foreign_key):
    table, column = foreign_key.parent.table, foreign_key.parent
    if foreign_key.column.table.name == "actions":
        return select(column).where(column == Action.id).correlate(Action).exists()
    return select(column).select_from(table).join(
        ExecutionAttempt, column == ExecutionAttempt.id,
    ).where(ExecutionAttempt.action_id == Action.id).correlate(Action).exists()


def require_unprotected_batch(session, action_ids, *, as_of) -> None:
    # Action rows are locked by selection; Attempt locks also serialize incoming FK references.
    session.execute(select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id.in_(action_ids),
    ).order_by(ExecutionAttempt.id).with_for_update()).all()
    predicates = protected_dependencies(as_of)
    blocked = session.scalar(select(Action.id).where(
        Action.id.in_(action_ids), or_(*(predicate for _name, predicate in predicates)),
    ).order_by(Action.id).limit(1))
    if blocked is not None:
        raise RuntimeError(f"runtime_retention_dependency_changed:{blocked}")
