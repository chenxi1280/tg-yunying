"""Read the original deadline without changing an Action or its reservations."""
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select

from app.models import Action, ExecutionAttempt, TaskDayLedger
from .datetime_compat import ensure_aware, utc_storage_as_beijing_wall
from .production_e4_identity import action_ledger_scope


OPEN_STATUSES = frozenset({"pending", "claiming", "executing", "retryable_failed"})
UNKNOWN_STATUSES = frozenset({"unknown_after_send", "closed_unknown"})


def task_queue_diagnostics(session, task):
    ledger = session.scalar(select(TaskDayLedger).where(
        TaskDayLedger.task_id == task.id, TaskDayLedger.tenant_id == task.tenant_id,
    ).order_by(TaskDayLedger.period_start_at.desc()).limit(1))
    if ledger is None:
        return {"status": "ledger_missing", "state_counts": {}}
    actions = list(session.scalars(select(Action).where(
        Action.tenant_id == task.tenant_id, Action.task_id == task.id,
        Action.task_lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
        Action.action_type == "send_message",
        Action.status.in_(OPEN_STATUSES | UNKNOWN_STATUSES),
        action_ledger_scope(session, ledger),
    )))
    states = original_deadline_states(session, ledger, actions)
    return {"status": "observed", "ledger_id": ledger.id,
        "original_deadline_at": ledger.deadline_at.isoformat(),
        "state_counts": deadline_state_counts(states)}


def original_deadline_states(session, ledger, actions, *, now=None):
    timestamp = now or datetime.now(timezone.utc)
    called = set(session.scalars(select(ExecutionAttempt.action_id).where(
        ExecutionAttempt.tenant_id == ledger.tenant_id,
        ExecutionAttempt.action_id.in_([action.id for action in actions]),
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).distinct()))
    deadline = ensure_aware(utc_storage_as_beijing_wall(ledger.deadline_at))
    return {
        action.id: original_deadline_state(action, deadline=deadline,
            called=action.id in called, now=timestamp)
        for action in actions
    }


def original_deadline_state(action, *, deadline, called, now):
    if action.status in UNKNOWN_STATUSES:
        return "unknown_preserved"
    if action.status not in OPEN_STATUSES:
        return "terminal"
    if called:
        return "called_history"
    if ensure_aware(now) >= ensure_aware(deadline):
        return "expired_uncalled"
    release = max(ensure_aware(value) for value in (
        action.scheduled_at, action.release_not_before_at, action.effective_claim_at,
    ) if value is not None)
    if release >= ensure_aware(deadline):
        return "outside_original_deadline"
    return "valid_wait"


def deadline_state_counts(states):
    return dict(sorted(Counter(states.values()).items()))
