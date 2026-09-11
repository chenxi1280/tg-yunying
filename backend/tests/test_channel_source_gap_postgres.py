"""Channel admissions retain valid message reservations without a ledger join."""
import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import ExecutionAttempt, OperationTarget, SourcePacingAdmission, SourcePacingState, Tenant, TgAccount
from app.services.task_center.source_pacing import wall_datetime
from app.services.task_center.source_pacing_admission import admit_source_paced_attempt
from tests.postgres_pacing_e4_fixture import factory
from tests.test_source_pacing_admission import NOW, _reaction_source_entities, _reaction_action_and_reservation

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]
GAP = timedelta(days=1)


def _seed(db):
    db.add(Tenant(id=1, name='channel gap'))
    db.flush()
    db.add_all([TgAccount(id=1, tenant_id=1, display_name='gap', phone_masked='***'),
                OperationTarget(id=10, tenant_id=1, target_type='channel', tg_peer_id='-1009001', title='gap')])
    db.flush()
    message, task, owner = _reaction_source_entities()
    for row in (message, task, owner):
        db.add(row)
        db.flush()
    action, reservation, deadline = _reaction_action_and_reservation(task, owner)
    action.scheduled_at = NOW
    db.add(action)
    db.flush()
    db.add(reservation)
    attempt = ExecutionAttempt(action_id=action.id, tenant_id=1, account_id=1, status='before_call')
    db.add(attempt)
    state = SourcePacingState(tenant_id=1, pacing_domain='reaction',
        source_key_hash=hashlib.sha256(b'-1009001').hexdigest(), next_call_not_before_at=deadline+GAP)
    db.add(state)
    db.flush()
    return action, attempt, state, deadline


def test_channel_stale_tail_does_not_exhaust_original_message_window(factory):
    with factory() as db:
        action, attempt, state, deadline = _seed(db)
        assert admit_source_paced_attempt(db, action, attempt, now_value=NOW)
        db.commit()
        admission = db.scalar(select(SourcePacingAdmission))
        assert wall_datetime(admission.call_not_before_at) == NOW
        assert admission.state == 'call_started'
        assert wall_datetime(action.release_not_before_at) == NOW
        assert wall_datetime(state.next_call_not_before_at) == deadline+GAP


def test_message_reservation_without_ledger_still_excludes_its_gap(factory):
    with factory() as db:
        action, attempt, state, deadline = _seed(db)
        assert not admit_source_paced_attempt(db, action, attempt, now_value=NOW-GAP)
        admission = db.scalar(select(SourcePacingAdmission))
        from app.services.task_center.source_pacing_gap import _future_reservations
        peer = SourcePacingAdmission(id='probe', tenant_id=1, source_pacing_state_id=state.id)
        assert _future_reservations(db, peer, timestamp=NOW-GAP) == ((NOW, int(GAP.total_seconds())),)
        admission.call_not_before_at = deadline
        db.flush()
        assert _future_reservations(db, peer, timestamp=NOW-GAP) == ()
        db.rollback()
