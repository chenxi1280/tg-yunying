from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (ChannelMessage, CommentFulfillmentObligation, Task,
    TaskAccountGroupBindingSetRevision, TaskDayLedger, TaskGroupDailyTarget,
    Tenant, TgGroup, ViewFulfillmentObligation)
from app.services.task_center.engagement_action_contract import action_uses_unified_contract
from app.services.task_center.engagement_runtime_resources import reserve_attempt_resources
from tests.test_engagement_action_contract import _binding_start, _lease_count
from tests.test_engagement_runtime_resources import _attempt, _seed, _session


pytestmark = pytest.mark.no_postgres
ACTION_TYPES = {"group_ai_chat": "send_message", "channel_comment": "post_comment",
                "channel_view": "view_message"}


def _legacy_day(session, task, before):
    ledger = TaskDayLedger(tenant_id=1, task_id=task.id, timezone_snapshot="Asia/Shanghai",
        timezone_revision=1, obligation_local_date=before.date(), period_start_at=before,
        deadline_at=before + timedelta(days=1), day_phase="full_day",
        planning_anchor_at=before, created_at=before)
    session.add(ledger)
    session.flush()
    return ledger


def _legacy_owner(session, task, before):
    message = ChannelMessage(tenant_id=1, channel_target_id=101, message_id=1)
    session.add(message)
    session.flush()
    if task.type == "channel_comment":
        return "comment_fulfillment_obligation_id", CommentFulfillmentObligation(
            tenant_id=1, task_id=task.id, channel_message_id=message.id,
            comment_plan_revision=1, target_ordinal=1, account_id=11, created_at=before)
    day = _legacy_day(session, task, before)
    if task.type == "channel_view":
        return "view_fulfillment_obligation_id", ViewFulfillmentObligation(
            tenant_id=1, task_day_ledger_id=day.id, channel_message_id=message.id,
            account_id=11, created_at=before)
    session.add(TgGroup(id=101, tenant_id=1, tg_peer_id="-100101", title="旧日目标"))
    session.flush()
    return "daily_group_target_id", TaskGroupDailyTarget(tenant_id=1,
        task_id=task.id, task_day_ledger_id=day.id, group_id=101, target_date=before.date(),
        configured_message_target=10, frozen_account_count=3, effective_message_target=10,
        daily_fulfillment_phase="full_day", scope_frozen_at=before,
        full_day_committed_at=before, created_at=before)


@pytest.mark.parametrize("task_type", ACTION_TYPES)
def test_old_quantity_owner_is_not_reinterpreted_for_later_action(task_type):
    with _session() as session:
        task = _seed(session)
        task.type = task_type
        before = _binding_start(session) - timedelta(seconds=1)
        key, owner = _legacy_owner(session, task, before)
        session.add(owner)
        session.flush()
        action, attempt = _attempt(session, task, 11, action_type=ACTION_TYPES[task_type])
        action.task_type = task_type
        action.payload = {key: owner.id}
        reserve_attempt_resources(session, action, attempt)
        assert _lease_count(session) == 0


def test_later_group_binding_does_not_move_initial_contract_boundary():
    with _session() as session:
        task = _seed(session)
        action, _ = _attempt(session, task, 11)
        first = session.scalar(select(TaskAccountGroupBindingSetRevision))
        first.state = "superseded"
        session.flush()
        session.add(TaskAccountGroupBindingSetRevision(tenant_id=1, task_id=task.id,
            task_lifecycle_epoch=task.task_lifecycle_epoch, binding_set_revision=2,
            account_group_ids=[1], binding_set_hash="successor",
            effective_from=action.created_at + timedelta(hours=1)))
        session.flush()
        assert action_uses_unified_contract(session, action)


def test_legacy_unknown_and_older_epoch_keep_their_original_identity():
    with _session() as session:
        task = _seed(session)
        action, _ = _attempt(session, task, 11)
        action.status = "unknown_after_send"
        action.result = {"remote_outcome": "unknown"}
        action.created_at = _binding_start(session) - timedelta(seconds=1)
        original = (action.status, dict(action.result), dict(action.payload))
        assert not action_uses_unified_contract(session, action)
        task.task_lifecycle_epoch += 1
        assert not action_uses_unified_contract(session, action)
        assert (action.status, action.result, action.payload) == original


@pytest.mark.parametrize("field,value,error", [
    ("account_id", 12, "engagement_action_work_account_mismatch"),
    ("tenant_id", 2, "engagement_action_work_owner_mismatch"),
    ("task_id", "other-task", "engagement_action_work_owner_mismatch"),
    ("task_lifecycle_epoch", 2, "engagement_action_work_epoch_mismatch"),
])
def test_mismatched_owner_cannot_turn_unified_action_into_legacy(field, value, error):
    with _session() as session:
        task = _seed(session)
        task.type = "channel_comment"
        key, owner = _legacy_owner(session, task, _binding_start(session) - timedelta(seconds=1))
        session.add(Tenant(id=2, name="其他租户"))
        session.flush()
        session.add(Task(id="other-task", tenant_id=2, type="channel_comment", name="其他任务"))
        session.flush()
        setattr(owner, field, value)
        session.add(owner)
        session.flush()
        action, _ = _attempt(session, task, 11)
        action.task_type = task.type
        action.payload = {key: owner.id}
        with pytest.raises(ValueError, match=error):
            action_uses_unified_contract(session, action)
