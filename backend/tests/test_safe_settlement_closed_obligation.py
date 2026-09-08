from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, ExecutionAttempt, FulfillmentObligationProjection, FulfillmentRemoteFact, Task, Tenant
from app.services.task_center.direct_action_claims import settle_fact_first_action_before_gateway
from app.services.task_center.fulfillment_remote_facts import ensure_action_obligation

NOW = datetime(2026, 9, 8, 9)


@pytest.fixture
def session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name='test'))
        current.add(Task(id='task', tenant_id=1, name='test', type='channel_comment', status='running'))
        current.commit()
        yield current


def action(session, aid, status):
    row = Action(id=aid, tenant_id=1, task_id='task', task_type='channel_comment',
                 action_type='post_comment', status=status, scheduled_at=NOW,
                 payload={'comment_fulfillment_obligation_id': 'comment-owner'})
    session.add(row)
    session.flush()
    return row


@pytest.mark.no_postgres
@pytest.mark.parametrize('state,winner_status', [('confirmed', 'success'), ('unknown', 'unknown_after_send'), ('open', 'pending')])
def test_rejected_loser_preserves_winner_without_new_fact(session, state, winner_status):
    winner = action(session, 'winner', winner_status)
    assert ensure_action_obligation(session, winner)
    projection = session.scalar(select(FulfillmentObligationProjection))
    projection.state = state
    version = projection.version
    loser = action(session, 'loser', 'pending')
    assert settle_fact_first_action_before_gateway(
        session, loser, now=NOW, reason_code='pacing_claim_deadline_exceeded', detail='expired',
    ) == set()
    session.flush()
    assert loser.status == 'skipped'
    assert loser.result['error_code'] in {'obligation_not_open', 'duplicate_open_obligation'}
    assert projection.state == state
    assert projection.active_action_id == winner.id
    assert projection.version == version
    assert winner.status == winner_status
    assert list(session.scalars(select(ExecutionAttempt))) == []
    assert list(session.scalars(select(FulfillmentRemoteFact))) == []


@pytest.mark.no_postgres
def test_unsafe_loser_is_not_suppressed(session):
    winner = action(session, 'winner', 'success')
    assert ensure_action_obligation(session, winner)
    session.scalar(select(FulfillmentObligationProjection)).state = 'confirmed'
    loser = action(session, 'loser', 'pending')
    session.add(ExecutionAttempt(tenant_id=1, action_id=loser.id, attempt_no=1,
                               status='unknown_after_send', gateway_call_started_at=NOW))
    session.flush()
    with pytest.raises(RuntimeError, match='remote_evidence_unsafe'):
        settle_fact_first_action_before_gateway(
            session, loser, now=NOW, reason_code='pacing_claim_deadline_exceeded', detail='expired',
        )
    assert loser.status == 'pending'


@pytest.mark.no_postgres
def test_closed_loser_releases_only_its_pacing_reservation(session):
    from app.models import AccountPacingReservation

    winner = action(session, 'winner', 'success')
    assert ensure_action_obligation(session, winner)
    projection = session.scalar(select(FulfillmentObligationProjection))
    projection.state = 'confirmed'
    loser = action(session, 'loser', 'pending')
    reservations = []
    for index, row in enumerate((winner, loser), start=1):
        reservation = AccountPacingReservation(
            tenant_id=1, task_id='task', account_id=index,
            pacing_slot_key=row.id, action_id=row.id, policy_version='test',
            due_at=NOW, release_not_before_at=NOW, effective_claim_at=NOW,
            state='reserved',
        )
        session.add(reservation)
        reservations.append(reservation)
    session.flush()
    settle_fact_first_action_before_gateway(
        session, loser, now=NOW, reason_code='pacing_claim_deadline_exceeded', detail='expired',
    )
    assert reservations[0].state == 'reserved'
    assert reservations[1].state == 'missed'
    assert projection.state == 'confirmed'
