"""Resume only never-dispatched membership rows from this task's started stage."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, FulfillmentRemoteFact, GatewayRequestEvidenceJournal, OperationTarget, Task
from app.services._common import _now

from .channel_membership_candidates import candidate_accounts_for_config
from .channel_membership_execution import MEMBERSHIP_ACTION_TYPES, START_EPOCH_KEY
from .source_pacing import wall_datetime


def resume_uncalled_channel_memberships(session: Session, task: Task, target: OperationTarget) -> None:
    previous_epoch = int((task.stats or {}).get(START_EPOCH_KEY) or 0)
    current_epoch = int(task.task_lifecycle_epoch or 1)
    if not previous_epoch or previous_epoch == current_epoch:
        return
    accounts = candidate_accounts_for_config(session, task.tenant_id, task.account_config or {})
    rows = list(session.scalars(select(Action).where(
        Action.task_id == task.id,
        Action.tenant_id == task.tenant_id,
        Action.task_type == task.type,
        Action.action_type.in_(MEMBERSHIP_ACTION_TYPES),
        Action.task_lifecycle_epoch == previous_epoch,
        Action.status == "pending",
        Action.executed_at.is_(None),
        Action.account_id.in_([account.id for account in accounts]),
        Action.payload["channel_target_id"].as_integer() == target.id,
        Action.payload["target_reference_revision"].as_integer() == int(target.reference_revision or 1),
        *[_no_action_evidence(model) for model in (ExecutionAttempt, FulfillmentRemoteFact, GatewayRequestEvidenceJournal)],
    ).order_by(Action.scheduled_at, Action.id).with_for_update()))
    rows = _recheck_evidence_after_lock(session, rows)
    if not rows:
        return
    shift = max(timedelta(0), _now() - wall_datetime(rows[0].scheduled_at))
    for action in rows:
        _resume_action(action, current_epoch=current_epoch, shift=shift)
    task.stats = {**dict(task.stats or {}), "membership_resumed_uncalled_count": len(rows)}


def _no_action_evidence(model):
    return ~select(model.action_id).where(model.action_id == Action.id).exists()


def _recheck_evidence_after_lock(session: Session, rows: list[Action]) -> list[Action]:
    if not rows:
        return []
    eligible_ids = set(session.scalars(select(Action.id).where(
        Action.id.in_([action.id for action in rows]),
        *[_no_action_evidence(model) for model in (ExecutionAttempt, FulfillmentRemoteFact, GatewayRequestEvidenceJournal)],
    )))
    return [action for action in rows if action.id in eligible_ids]


def _resume_action(action: Action, *, current_epoch: int, shift: timedelta) -> None:
    previous_epoch = action.task_lifecycle_epoch
    previous_time = action.scheduled_at
    action.task_lifecycle_epoch = current_epoch
    action.action_version = int(action.action_version or 1) + 1
    action.scheduled_at = wall_datetime(previous_time) + shift
    action.result = {
        **dict(action.result or {}),
        "membership_resumed_from_epoch": previous_epoch,
        "membership_previous_scheduled_at": previous_time.isoformat(),
        "membership_schedule_shift_seconds": shift.total_seconds(),
    }
