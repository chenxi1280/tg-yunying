"""A proved later success does not erase or inherit historical unknown calls."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import ExecutionAttempt, FulfillmentObligationProjection, FulfillmentRemoteFact, ReactionRemoteFact, GatewayRequestEvidenceJournal
from app.services.task_center.channel_fulfillment_identity import reaction_state_revision
from app.services.task_center.channel_remote_evidence import action_remote_mutation_evidence
from app.services.task_center.fulfillment_remote_facts import ensure_action_obligation, persist_remote_fact, project_remote_fact
from tests.test_source_pacing_admission import NOW, session, _reaction_source_entities, _reaction_action_and_reservation

pytestmark = pytest.mark.no_postgres


def seed(session):
    message, task, obligation = _reaction_source_entities()
    action, reservation, _ = _reaction_action_and_reservation(task, obligation)
    action.status = "pending"
    action.payload = {**action.payload, "channel_id": "-1009001",
        "channel_message_id": message.id, "reaction_emoji": "👍"}
    for row in (message, task, obligation, action, reservation):
        session.add(row)
        session.flush()
    assert ensure_action_obligation(session, action)
    old = ExecutionAttempt(id="old-unknown", tenant_id=1, account_id=1, action_id=action.id,
        task_lifecycle_epoch=action.task_lifecycle_epoch, attempt_no=1, status="result_unknown",
        gateway_call_started_at=NOW, after_call_at=NOW+timedelta(seconds=1))
    session.add(old)
    action.status = "unknown_after_send"
    session.flush()
    unknown = persist_remote_fact(session, action)
    project_remote_fact(session, unknown)
    fact = ReactionRemoteFact(id="typed-success", tenant_id=1, obligation_id=obligation.id,
        target_peer_id="-1009001", channel_message_id=message.id, account_id=1,
        reaction_state_revision=reaction_state_revision("👍"), reaction_evidence_hash="a"*64,
        remote_confirmed_at=NOW+timedelta(seconds=11))
    good = ExecutionAttempt(id="proved-success", tenant_id=1, account_id=1, action_id=action.id,
        task_lifecycle_epoch=action.task_lifecycle_epoch, attempt_no=2, status="success",
        gateway_call_started_at=NOW+timedelta(seconds=10), after_call_at=NOW+timedelta(seconds=11),
        result_snapshot={"remote_fact_id":"wire-receipt"})
    session.add_all((fact, good))
    session.flush()
    session.add(GatewayRequestEvidenceJournal(tenant_id=1,action_id=action.id,
        execution_attempt_id=good.id,account_id=1,gateway_request_identity="neutral-success",
        request_fingerprint="r"*64,target_fingerprint="t"*64,result_fingerprint="s"*64,
        evidence_hash="e"*64,remote_fact_id=fact.id,remote_mutation_state="true"))
    action.status = "success"
    action.result = {"success":True,"remote_fact_id":"wire-receipt"}
    session.flush()
    return action, good, fact, unknown


def test_typed_success_wins_for_projection_but_unknown_retry_risk_remains(session):
    action, good, _, unknown = seed(session)
    fact = persist_remote_fact(session, action)
    assert fact.fact_kind == "reaction_observed"
    assert fact.attempt_id == good.id
    assert persist_remote_fact(session, action).fact_id == fact.fact_id
    project_remote_fact(session, fact)
    project_remote_fact(session, unknown)
    assert session.scalar(select(FulfillmentObligationProjection)).state == "confirmed"
    assert action_remote_mutation_evidence(session, action).state == "unknown"
    assert session.get(ExecutionAttempt,"old-unknown").status == "result_unknown"
    assert len(list(session.scalars(select(FulfillmentRemoteFact)))) == 2


@pytest.mark.parametrize("field,value",[("account_id",2),("tenant_id",2),
    ("channel_message_id",999),("target_peer_id","-100other"),
    ("obligation_id","other"),("reaction_state_revision","wrong")])
def test_mismatched_typed_fact_does_not_override_unknown(session, field, value):
    action, _, fact, unknown = seed(session)
    setattr(fact,field,value)
    session.flush()
    assert persist_remote_fact(session,action).fact_id == unknown.fact_id


@pytest.mark.parametrize("field,value",[("account_id",2),("task_lifecycle_epoch",99),
    ("gateway_call_started_at",NOW+timedelta(seconds=12))])
def test_mismatched_attempt_does_not_inherit_success(session,field,value):
    action,good,_,unknown=seed(session)
    setattr(good,field,value)
    session.flush()
    assert persist_remote_fact(session,action).fact_id == unknown.fact_id


def test_missing_typed_receipt_does_not_promote_success_string(session):
    action,_,fact,unknown=seed(session)
    session.delete(fact)
    session.flush()
    assert persist_remote_fact(session,action).fact_id == unknown.fact_id


def test_wrong_tenant_cannot_project_into_another_tenants_obligation(session):
    _,_,_,unknown=seed(session)
    unknown.tenant_id=2
    session.flush()
    with pytest.raises(RuntimeError,match="remote_fact_obligation_projection_missing"):
        project_remote_fact(session,unknown)
