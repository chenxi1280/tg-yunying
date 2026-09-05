from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (AccountPoolConcurrencyLease, ChannelMessage,
    ReactionFulfillmentObligation, TaskAccountGroupBindingSetRevision)
from app.services.task_center.engagement_runtime_resources import reserve_attempt_resources
from app.timezone import as_beijing
from tests.test_engagement_runtime_resources import _attempt, _seed, _session


pytestmark = pytest.mark.no_postgres


def _binding_start(session):
    binding = session.scalar(select(TaskAccountGroupBindingSetRevision))
    return as_beijing(binding.effective_from)


def _lease_count(session):
    return session.scalar(select(func.count()).select_from(AccountPoolConcurrencyLease))


def test_legacy_action_is_not_reinterpreted_by_current_unified_flag():
    with _session() as session:
        task = _seed(session)
        action, attempt = _attempt(session, task, 11)
        action.created_at = _binding_start(session) - timedelta(seconds=1)
        original = (dict(action.payload), dict(action.result), action.status, action.scheduled_at)
        reserve_attempt_resources(session, action, attempt)
        assert _lease_count(session) == 0
        assert (action.payload, action.result, action.status, action.scheduled_at) == original


def test_unified_action_keeps_resources_after_task_flag_changes():
    with _session() as session:
        task = _seed(session)
        action, attempt = _attempt(session, task, 11)
        task.type_config = {**task.type_config, "engagement_contract_version": "legacy_v0"}
        reserve_attempt_resources(session, action, attempt)
        assert _lease_count(session) == 1


def test_new_action_for_old_reaction_obligation_keeps_legacy_contract():
    with _session() as session:
        task = _seed(session)
        previous_time = _binding_start(session) - timedelta(seconds=1)
        message = ChannelMessage(tenant_id=1, channel_target_id=101, message_id=1)
        session.add(message)
        session.flush()
        obligation = ReactionFulfillmentObligation(tenant_id=1, task_id=task.id,
            channel_message_id=message.id, account_id=11, reaction_contract_version=1,
            task_lifecycle_epoch=task.task_lifecycle_epoch, created_at=previous_time)
        session.add(obligation)
        session.flush()
        action, attempt = _attempt(session, task, 11)
        action.payload = {"reaction_fulfillment_obligation_id": obligation.id}
        reserve_attempt_resources(session, action, attempt)
        assert _lease_count(session) == 0
