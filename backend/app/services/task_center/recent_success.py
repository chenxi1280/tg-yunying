"""Read rolling confirmation counts; never mutate task fulfillment ledgers."""
from datetime import timedelta

from sqlalchemy import func, select

from app.models import Action, ExecutionAttempt, FulfillmentRemoteFact
from app.services._common import _now
from app.timezone import as_beijing_aware


from .success_fact_query import SUCCESS_KINDS, WINDOW_HOURS, valid_success_predicates


def recent_task_success(session, task, *, now_value=None) -> dict | None:
    if task.type not in SUCCESS_KINDS:
        return None
    current = as_beijing_aware(now_value or _now())
    start = current - timedelta(hours=WINDOW_HOURS)
    first = _ranked_successes(task).subquery()
    rows = session.execute(select(first.c.account_id, func.count()).where(
        first.c.ordinal == 1,
        first.c.confirmed_at >= start,
        first.c.confirmed_at <= current,
    ).group_by(first.c.account_id).order_by(first.c.account_id)).all()
    return {
        "window_hours": WINDOW_HOURS,
        "window_start": start.isoformat(), "window_end": current.isoformat(),
        "time_basis": "original_call_confirmation",
        "metric_label": SUCCESS_KINDS[task.type][2],
        "success_count": sum(count for _account, count in rows),
        "unassigned_count": sum(count for account, count in rows if account is None),
        "account_counts": [{"account_id": account, "success_count": count}
                           for account, count in rows if account is not None],
    }


def _ranked_successes(task):
    fact = FulfillmentRemoteFact
    predicates = (
        *valid_success_predicates(),
        fact.tenant_id == task.tenant_id,
        fact.task_id == task.id,
        fact.task_type == task.type,
    )
    return select(
        ExecutionAttempt.account_id,
        fact.observed_at.label("confirmed_at"),
        func.row_number().over(partition_by=fact.action_id,
            order_by=(fact.observed_at, fact.fact_id)).label("ordinal"),
    ).select_from(fact).join(Action, Action.id == fact.action_id).join(
        ExecutionAttempt, ExecutionAttempt.id == fact.attempt_id,
    ).where(*predicates)
