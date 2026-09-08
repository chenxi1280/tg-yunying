"""Persist fan-out before independently waking each current bound task."""
import logging
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from app.models import (
    AccountGroupMembershipRevision, AccountGroupStateRevision, StageWakeOutbox,
    Task, TaskAccountGroupBindingSetRevision,
)
from app.services.account_group_revisions import MEMBERSHIP_WAKE_STAGE
from app.timezone import as_beijing, as_beijing_aware
from .engagement_binding import ENGAGEMENT_TASK_TYPES, UNIFIED_ENGAGEMENT_CONTRACT_VERSION
from .planner_wake import wake_task_planner

TASK_MEMBERSHIP_WAKE_STAGE = "refresh_task_membership"
TASK_WAKE_AGGREGATE = "account_group_task"
REVISION_MODELS = {"account_group_membership": AccountGroupMembershipRevision,
    "account_group_state": AccountGroupStateRevision}
logger = logging.getLogger(__name__)


def consume_membership_wake(session, wake_id, current):
    current = as_beijing_aware(current)
    wake = _locked_wake(session, wake_id)
    if not _due(wake, MEMBERSHIP_WAKE_STAGE, current):
        return 0
    revision = _revision(session, wake)
    if revision is None:
        return _invalid(wake, current)
    candidates = session.execute(_binding_candidates(revision.tenant_id))
    for task_id, epoch, groups in candidates:
        if revision.account_pool_id not in groups:
            continue
        identity = f"{wake.id}:{task_id}"
        session.add(StageWakeOutbox(id=str(uuid5(NAMESPACE_URL, f"{identity}:{epoch}")),
            tenant_id=wake.tenant_id, aggregate_type=TASK_WAKE_AGGREGATE, aggregate_id=identity,
            aggregate_revision=epoch, stage=TASK_MEMBERSHIP_WAKE_STAGE,
            state="pending", available_at=current))
    wake.state = "expanded"
    wake.attempt_count += 1
    session.flush()
    return 0


def consume_task_membership_wake(session, wake_id, current):
    current = as_beijing_aware(current)
    wake = _locked_wake(session, wake_id)
    if not _due(wake, TASK_MEMBERSHIP_WAKE_STAGE, current):
        return 0
    parent_id, separator, task_id = wake.aggregate_id.partition(":")
    parent = session.get(StageWakeOutbox, parent_id) if separator else None
    revision = _revision(session, parent) if parent is not None and parent.tenant_id == wake.tenant_id else None
    if revision is None or wake.aggregate_type != TASK_WAKE_AGGREGATE:
        return _invalid(wake, current)
    task = session.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == wake.tenant_id)
        .with_for_update().execution_options(populate_existing=True))
    if task is None or not _still_bound(session, task, revision, epoch=wake.aggregate_revision):
        wake.state = "superseded"
    else:
        wake_task_planner(session, task, reason_code=parent.aggregate_type + "_changed", not_before_at=current)
        wake.state = "delivered"
    wake.delivered_at = current
    wake.attempt_count += 1
    session.flush()
    return 1


def settle_membership_wake(session, wake_id, current):
    current = as_beijing_aware(current)
    parent = _locked_wake(session, wake_id)
    if parent is None or parent.state != "expanded":
        return 0
    states = set(session.scalars(select(StageWakeOutbox.state).where(
        StageWakeOutbox.tenant_id == parent.tenant_id,
        StageWakeOutbox.stage == TASK_MEMBERSHIP_WAKE_STAGE,
        StageWakeOutbox.aggregate_id.like(parent.id + ":%"))))
    if "pending" in states:
        parent.available_at = current
        return 0
    parent.state = "failed" if states & {"failed", "invalid"} else "delivered"
    parent.delivered_at = current
    return int(parent.state == "delivered")


def _locked_wake(session, wake_id):
    return session.scalar(select(StageWakeOutbox).where(StageWakeOutbox.id == wake_id)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True))


def _due(wake, stage, current):
    return (wake is not None and wake.stage == stage and wake.state == "pending"
        and as_beijing(wake.available_at) <= as_beijing(current))


def _revision(session, wake):
    model = REVISION_MODELS.get(wake.aggregate_type)
    revision = session.get(model, wake.aggregate_id) if model else None
    if revision is None or (revision.tenant_id, revision.revision) != (wake.tenant_id, wake.aggregate_revision):
        return None
    return revision


def _invalid(wake, current):
    logger.error("account_group_wake_revision_invalid wake_id=%s aggregate_id=%s", wake.id, wake.aggregate_id)
    wake.state, wake.delivered_at = "invalid", current
    wake.attempt_count += 1
    return 0


def _still_bound(session, task, revision, *, epoch):
    if int(task.task_lifecycle_epoch or 1) != epoch:
        return False
    row = session.execute(_binding_candidates(revision.tenant_id).where(Task.id == task.id)).first()
    return row is not None and revision.account_pool_id in row.account_group_ids


def _binding_candidates(tenant_id):
    binding = TaskAccountGroupBindingSetRevision
    return select(Task.id, Task.task_lifecycle_epoch, binding.account_group_ids).join(binding,
        binding.task_id == Task.id).where(Task.tenant_id == tenant_id,
            binding.tenant_id == tenant_id, Task.status == "running", Task.deleted_at.is_(None),
            Task.type.in_(ENGAGEMENT_TASK_TYPES), binding.state == "active",
            binding.task_lifecycle_epoch == Task.task_lifecycle_epoch,
            Task.type_config["engagement_contract_version"].as_string() == UNIFIED_ENGAGEMENT_CONTRACT_VERSION)
