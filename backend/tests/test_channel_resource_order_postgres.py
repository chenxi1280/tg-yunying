"""Pre-Gateway resource failures cannot consume a channel source-call interval."""
from types import SimpleNamespace
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (AccountPool, AccountPoolConcurrencyPolicyRevision,
    AccountBehaviorBudgetPolicyRevision, ExecutionResiliencePolicyRevision,
    ExecutionAttempt, ReactionFulfillmentObligation, SourcePacingAdmission,
    Task, TgAccount)
from app.services.task_center import dispatcher
from tests.postgres_pacing_e4_fixture import factory
from tests.test_channel_source_gap_postgres import _seed, NOW
from tests.test_reaction_creation_origin_postgres import _plans

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def _scope(db, monkeypatch, *, resources_ready):
    from app.services import outbound_target_gate
    monkeypatch.setattr(outbound_target_gate, 'evaluate_outbound_target_gate', lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher, '_now', lambda: NOW)
    from app.services.task_center import source_pacing_admission
    monkeypatch.setattr(source_pacing_admission, '_now', lambda: NOW)
    action, _, state, _ = _seed(db)
    account = db.get(TgAccount, 1)
    account.status = '在线'
    account.session_ciphertext = 'QA-only-no-network'
    task = db.get(Task, action.task_id)
    task.type_config = {'engagement_contract_version': 'unified_engagement_v1'}
    if resources_ready:
        db.add(AccountPool(id=2, tenant_id=1, name='original'))
        db.flush()
        account.pool_id = 2
        owner = db.scalar(select(ReactionFulfillmentObligation))
        _plans(db, action, owner)
        owner.release_not_before_at = NOW+timedelta(hours=1)
        db.add_all([
            AccountPoolConcurrencyPolicyRevision(tenant_id=1, account_pool_id=2, hard_remote_inflight_limit=5),
            AccountBehaviorBudgetPolicyRevision(tenant_id=1, account_class='normal', action_budgets={'total':5,'reaction':5}),
            ExecutionResiliencePolicyRevision(tenant_id=1),
        ])
    db.commit()
    return action, account, state


@pytest.mark.parametrize('resources_ready', [False, True])
def test_pre_gateway_channel_deferral_preserves_source_and_settles_resources(factory, monkeypatch, resources_ready):
    with factory() as db:
        action, account, state = _scope(db, monkeypatch, resources_ready=resources_ready)
        payload = SimpleNamespace(channel_id='-1009001', target_reference_revision=1,
                                  target_reference_snapshot={'tg_peer_id':'-1009001'})
        assert dispatcher._reserve_channel_action_attempt(db, action, account, payload) is None
        db.refresh(state)
        assert state.last_call_started_at is None
        attempt = db.scalar(select(ExecutionAttempt).where(ExecutionAttempt.action_id == action.id)
                            .order_by(ExecutionAttempt.attempt_no.desc()).limit(1))
        assert attempt.gateway_call_started_at is None and attempt.status == 'skipped_before_gateway'
        if not resources_ready:
            assert attempt.failure_type == 'engagement_account_pool_missing'
            assert db.scalar(select(SourcePacingAdmission)) is None
            return
        assert attempt.failure_type == 'pacing_source_not_before'
        from app.services.task_center.engagement_runtime_resources import _attempt_resources
        lease, reservation, fence = _attempt_resources(db, attempt.id)
        assert lease.state == 'released' and reservation.state == 'released' and fence.state == 'terminal'
