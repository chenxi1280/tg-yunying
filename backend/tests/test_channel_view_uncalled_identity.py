from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Action, ChannelViewDailyIdentityOwner, ExecutionAttempt, FulfillmentRemoteFact, TgAccount, ViewRemoteFact
from app.services.task_center import dispatcher
from app.services.task_center.channel_view_daily_identity import release_daily_identity
from app.services.task_center.direct_action_claims import _claim_rows, settle_fact_first_action_before_gateway
from app.services.task_center.executors.channel_view import build_plan
from app.services.task_center.gateway_evidence_journal import (
    GatewayResultEvidence, bind_gateway_request_identity, record_gateway_result_evidence,
)
from app.services.task_center.payloads import validate_action_payload
from app.services.task_center.task_retirement import TaskGatewayFenced
from app.timezone import BEIJING_TZ
from tests.test_channel_view_daily_identity_lifecycle import _bind_pacing_reservation, _seed_two_tasks, _set_view_clock, new_session

pytestmark = pytest.mark.no_postgres
CURRENT = datetime(2026, 9, 8, 10, tzinfo=BEIJING_TZ)


def _seed(session, monkeypatch):
    _set_view_clock(monkeypatch, CURRENT)
    first, _ = _seed_two_tasks(session, channel_id=891, current=CURRENT)
    assert build_plan(session, first) == 1
    action = session.scalar(select(Action).where(Action.task_id == first.id))
    owner = session.scalar(select(ChannelViewDailyIdentityOwner).where(ChannelViewDailyIdentityOwner.action_id == action.id))
    return first, action, owner


def _uncalled_attempt(session, action):
    attempt = ExecutionAttempt(tenant_id=1, action_id=action.id, account_id=action.account_id,
        status="skipped_before_gateway", attempt_no=1, failure_type="task_lifecycle_admission_busy")
    session.add(attempt)
    session.flush()
    return attempt


def test_actual_view_reservation_fenced_before_call_does_not_publish_call_issued(monkeypatch):
    with new_session() as session:
        task, action, owner = _seed(session, monkeypatch)
        action.pacing_contract_version = ""
        task.status = "paused"
        session.commit()
        with pytest.raises(TaskGatewayFenced):
            dispatcher._reserve_channel_action_attempt(session, action,
                session.get(TgAccount, action.account_id), validate_action_payload(action.action_type, action.payload))
        session.commit()
        session.refresh(owner)
        attempt = session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.action_id == action.id))
        assert owner.state == "pre_gateway"
        assert attempt.status == "skipped_before_gateway" and attempt.gateway_call_started_at is None


def test_historical_early_projection_settles_without_replaying_telegram(monkeypatch):
    with new_session() as session:
        _, action, owner = _seed(session, monkeypatch)
        owner.state = "call_issued"
        old = _uncalled_attempt(session, action)
        session.commit()
        settle_fact_first_action_before_gateway(session, action, now=CURRENT,
            reason_code="pacing_claim_deadline_exceeded", detail="expired uncalled action")
        session.commit()
        assert owner.state == "available" and owner.action_id is None
        assert old.status == "skipped_before_gateway" and old.gateway_call_started_at is None
        assert action.status == "skipped"
        facts = session.scalars(select(FulfillmentRemoteFact).where(FulfillmentRemoteFact.action_id == action.id)).all()
        assert len(facts) == 1 and facts[0].fact_kind == "safely_not_executed"


def test_expired_uncalled_view_no_longer_rolls_back_healthy_batch_claim(monkeypatch):
    with new_session() as session:
        task, action, owner = _seed(session, monkeypatch)
        owner.state = "call_issued"
        _uncalled_attempt(session, action)
        reservation = _bind_pacing_reservation(session, action, current=CURRENT)
        reservation.source_deadline_at = CURRENT - timedelta(minutes=1)
        healthy = Action(tenant_id=1, task_id=task.id, task_type="channel_view", action_type="view_message",
            account_id=action.account_id, scheduled_at=CURRENT, payload={})
        session.add(healthy)
        session.commit()
        rows = [(row.id, row.task_id, row.action_version) for row in (action, healthy)]
        claimed = _claim_rows(session, rows, owner="QA", token="QA", now=CURRENT, lease_seconds=60)
        session.commit()
        assert claimed == [healthy.id] and healthy.status == "claiming"
        assert action.status == "skipped" and owner.state == "available"


@pytest.mark.parametrize("unsafe", ["unknown", "called", "inflight", "journal", "view_fact", "no_attempt"])
def test_projection_is_not_released_without_complete_uncalled_proof(monkeypatch, unsafe):
    with new_session() as session:
        _, action, owner = _seed(session, monkeypatch)
        owner.state = "call_issued"
        if unsafe != "no_attempt":
            attempt = _uncalled_attempt(session, action)
            _make_unsafe(session, action, owner, attempt=attempt, unsafe=unsafe)
        session.commit()
        assert release_daily_identity(session, action) is False
        assert owner.state == "call_issued" and owner.action_id == action.id


def _make_unsafe(session, action, owner, *, attempt, unsafe):
    if unsafe == "unknown":
        attempt.status = "result_unknown"
    elif unsafe == "called":
        attempt.gateway_call_started_at = CURRENT
    elif unsafe == "inflight":
        attempt.status = "before_call"
    elif unsafe == "journal":
        bind_gateway_request_identity(action, attempt)
        record_gateway_result_evidence(session, action, attempt, GatewayResultEvidence(remote_mutation_started=None))
    elif unsafe == "view_fact":
        session.add(ViewRemoteFact(tenant_id=1, obligation_id=owner.obligation_id,
            obligation_local_date=owner.obligation_local_date, target_peer_id=owner.target_peer_id,
            channel_message_id=owner.channel_message_id, account_id=owner.account_id, remote_confirmed_at=CURRENT))
