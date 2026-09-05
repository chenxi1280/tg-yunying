"""Deliver group revision wakes in separate transactions with Task-first ownership."""
import logging

from sqlalchemy import select, text

from app.models import (
    AccountGroupMembershipRevision, AccountGroupStateRevision, StageWakeOutbox,
    Task, TaskAccountGroupBindingSetRevision,
)
from app.services._common import _now
from app.services.account_group_revisions import MEMBERSHIP_WAKE_STAGE
from app.timezone import as_beijing
from .engagement_binding import ENGAGEMENT_TASK_TYPES, UNIFIED_ENGAGEMENT_CONTRACT_VERSION
from .planner_wake import wake_task_planner


WAKE_LOCK_TIMEOUT_MS = 100
REVISION_MODELS = {"account_group_membership": AccountGroupMembershipRevision,
    "account_group_state": AccountGroupStateRevision}
logger = logging.getLogger(__name__)


def drain_membership_wake_transactions(session_factory, *, limit=100):
    current = _now()
    with session_factory() as session:
        ids = list(session.scalars(select(StageWakeOutbox.id).where(
            StageWakeOutbox.stage == MEMBERSHIP_WAKE_STAGE,
            StageWakeOutbox.state == "pending", StageWakeOutbox.available_at <= current)
            .order_by(StageWakeOutbox.available_at, StageWakeOutbox.created_at).limit(limit)))
    processed = 0
    for wake_id in ids:
        try:
            with session_factory() as session:
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(text("SELECT set_config('lock_timeout', :timeout, true)"),
                        {"timeout": f"{WAKE_LOCK_TIMEOUT_MS}ms"})
                changed = consume_membership_wake(session, wake_id, current)
                session.commit()
                processed += changed
        except Exception:
            logger.exception("membership_wake_transaction_failed wake_id=%s", wake_id)
    return processed


def consume_membership_wake(session, wake_id, current):
    wake = session.scalar(select(StageWakeOutbox).where(StageWakeOutbox.id == wake_id)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if (wake is None or wake.stage != MEMBERSHIP_WAKE_STAGE or wake.state != "pending"
            or as_beijing(wake.available_at) > as_beijing(current)):
        return 0
    model = REVISION_MODELS.get(wake.aggregate_type)
    revision = session.get(model, wake.aggregate_id) if model else None
    if revision is None or (revision.tenant_id, revision.revision) != (
            wake.tenant_id, wake.aggregate_revision):
        raise ValueError("account_group_wake_revision_invalid")
    for task in _current_bound_tasks(session, revision):
        wake_task_planner(session, task, reason_code=wake.aggregate_type + "_changed",
            not_before_at=current)
    wake.state, wake.delivered_at = "delivered", current
    wake.attempt_count += 1
    session.flush()
    return 1


def _current_bound_tasks(session, revision):
    query = _binding_candidates(revision.tenant_id)
    candidates = session.execute(query)
    ids = [task_id for task_id, groups in candidates if revision.account_pool_id in groups]
    tasks = list(session.scalars(select(Task).where(Task.id.in_(ids)).order_by(Task.id)
        .with_for_update().execution_options(populate_existing=True)))
    current = {task_id for task_id, groups in session.execute(query.where(Task.id.in_(ids)))
        if revision.account_pool_id in groups}
    return [task for task in tasks if task.id in current
        and (task.type_config or {}).get("engagement_contract_version") == UNIFIED_ENGAGEMENT_CONTRACT_VERSION]


def _binding_candidates(tenant_id):
    binding = TaskAccountGroupBindingSetRevision
    return select(Task.id, binding.account_group_ids).join(binding,
        binding.task_id == Task.id).where(Task.tenant_id == tenant_id,
            binding.tenant_id == tenant_id, Task.status == "running", Task.deleted_at.is_(None),
            Task.type.in_(ENGAGEMENT_TASK_TYPES), binding.state == "active",
            binding.task_lifecycle_epoch == Task.task_lifecycle_epoch)
