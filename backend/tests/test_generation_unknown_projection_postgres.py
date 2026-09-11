from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, GenerationJob, Task, Tenant, FulfillmentRemoteFact
from app.services._common import _now
from app.services.task_center.ai_generation_recovery import (
    recover_stale_pre_gateway_generation,
)
from tests.owner_postgres_support import owner_engine

pytestmark = pytest.mark.isolated_postgres


@pytest.fixture
def scope(owner_engine):
    with Session(owner_engine) as db:
        db.add(Tenant(id=1, name='generation'))
        db.flush()
        db.add(Task(id='task', tenant_id=1, name='generation', type='group_ai_chat', status='running'))
        db.flush()
        job = GenerationJob(id='job', tenant_id=1, task_id='task', obligation_type='quantity_slot',
                            obligation_id='slot', state='unknown', job_version=6,
                            generation_sequence=1, context_snapshot_version=1,
                            generation_owner_id='', lease_expires_at=None)
        action = Action(id='action', tenant_id=1, task_id='task', task_type='group_ai_chat',
                        action_type='send_message', status='executing', obligation_type='quantity_slot',
                        obligation_id='slot', claim_owner='recovery', claim_token='claim',
                        lease_owner='old-generator', lease_expires_at=_now()-timedelta(minutes=1),
                        payload={'generation_job_id': 'job', 'ai_generation_status': 'generating',
                                 'ai_generation_claim_owner': 'old-generator'}, result={})
        db.add_all([job, action])
        db.commit()
        yield db, action, job


def test_unknown_job_repairs_stale_action_without_retry_or_job_change(scope):
    db, action, job = scope
    assert recover_stale_pre_gateway_generation(action, db)
    db.commit()
    assert action.payload['ai_generation_status'] == 'ai_result_persist_unknown'
    assert action.status == 'pending'
    assert action.claim_owner == 'recovery' and action.claim_token == 'claim'
    assert action.lease_owner == ''
    assert job.state == 'unknown' and job.job_version == 6
    assert not recover_stale_pre_gateway_generation(action, db)


@pytest.mark.parametrize('conflict', ['successor', 'owner', 'gateway', 'gateway_stage', 'identity', 'fact'])
def test_conflicting_unknown_job_never_resets_to_pending(scope, conflict):
    db, action, job = scope
    if conflict == 'successor':
        db.add(Action(id='successor', tenant_id=1, task_id='task', task_type='group_ai_chat',
                      action_type='send_message', obligation_type='quantity_slot', obligation_id='slot',
                      action_version=2, status='failed', payload={'generation_job_id': 'job'}))
    if conflict == 'owner':
        job.generation_owner_id = 'new-generator'
    if conflict == 'gateway':
        db.add(ExecutionAttempt(action_id=action.id, gateway_call_started_at=_now(), status='result_unknown'))
    if conflict == 'gateway_stage':
        job.generation_stage = 'gateway_reconcile_required'
    if conflict == 'fact':
        db.add(FulfillmentRemoteFact(tenant_id=1, task_id='task', task_type='group_ai_chat',
            obligation_type='quantity_slot', obligation_id='slot', action_id=action.id,
            attempt_id='attempt', mutation_kind='send_message', remote_mutation_key_hash='mutation',
            gateway_request_hash='request', fact_kind='remote_outcome_unknown', fact_identity_hash='fact'))
    if conflict == 'identity':
        job.obligation_id = 'other-slot'
    db.commit()
    with pytest.raises(RuntimeError):
        recover_stale_pre_gateway_generation(action, db)
    db.rollback()
    assert action.payload['ai_generation_status'] == 'generating'
    assert job.state == 'unknown' and job.job_version == 6
