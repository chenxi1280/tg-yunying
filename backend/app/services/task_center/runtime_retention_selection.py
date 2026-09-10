"""One indexed candidate selector for retention preview and execution."""
from collections import Counter

from sqlalchemy import and_, func, or_, select
from sqlalchemy.sql.selectable import Exists

from app.models import Action

from .runtime_retention_protection import protected_dependencies


def expired_action_predicate(cutoffs):
    age = func.coalesce(Action.executed_at, Action.scheduled_at, Action.created_at)
    return or_(*(and_(Action.status == status, age < cutoff)
                 for status, cutoff in cutoffs.items()))


def runtime_detail_batch(session, cutoffs, batch_size, *, as_of, lock=True) -> list:
    age = func.coalesce(Action.executed_at, Action.scheduled_at, Action.created_at)
    statement = select(Action.id, Action.task_id, Action.account_id, Action.task_type,
        Action.action_type, Action.status, Action.result, Action.executed_at,
        Action.scheduled_at, Action.created_at, age.label("age_at"),
        _target_dimension()).where(expired_action_predicate(cutoffs),
        *(_not_protected(predicate) for _name, predicate in protected_dependencies(as_of)),
    ).order_by(age.asc(), Action.created_at.asc(), Action.id.asc()).limit(batch_size)
    if lock:
        statement = statement.with_for_update(of=Action, skip_locked=True)
    return list(session.execute(statement))


def _not_protected(predicate):
    # EXISTS is never NULL; wrapping it prevents PostgreSQL anti-join planning.
    return ~predicate if isinstance(predicate, Exists) else ~func.coalesce(predicate, False)


def _target_dimension():
    keys = ("operation_target_id", "target_operation_target_id", "group_id",
            "channel_target_id", "chat_id")
    return func.coalesce(*(Action.payload[key].as_string() for key in keys), "").label("target_dimension")


def preview_protected_dependencies(session, cutoffs, *, as_of, batch_size) -> dict:
    """Report the explicit first expired batch, never imply these are global counts."""
    age = func.coalesce(Action.executed_at, Action.scheduled_at, Action.created_at)
    predicates = protected_dependencies(as_of)
    rows = session.execute(select(Action.id,
        *(func.coalesce(predicate, False).label(name) for name, predicate in predicates),
    ).where(expired_action_predicate(cutoffs)).order_by(
        age.asc(), Action.created_at.asc(), Action.id.asc(),
    ).limit(batch_size)).all()
    counts = Counter(name for row in rows for name, value in zip(
        (name for name, _predicate in predicates), row[1:],
    ) if value)
    return {"scope": "first_expired_batch", "scanned_count": len(rows),
            "scan_limit": batch_size, "reference_counts": dict(counts)}
