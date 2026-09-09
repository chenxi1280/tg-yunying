from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPacingReservation, Action, AuditLog, ChannelMessage, ExecutionAttempt,
    FulfillmentRemoteFact, GatewayRequestEvidenceJournal, OperationTarget, ReactionFulfillmentObligation,
    ReactionRemoteFact, Task, Tenant, TgAccount,
)
from app.services._common import _now
from app.services.task_center.account_pacing_reservations import bind_account_pacing_reservation
from app.services.task_center.channel_fulfillment import bind_obligation_action
from app.services.task_center.fulfillment_remote_facts import ensure_action_obligation
from app.services.task_center.reaction_backlog_replan import (
    BacklogReplanOperation, apply_reaction_backlog, verify_reaction_backlog,
)
from app.services.task_center.reaction_backlog_snapshot import preview_reaction_backlog, timeline_hash
from app.services.task_center.payloads import LikeMessagePayload, create_like_action, _action_dedupe_key, _plan_batch_key


pytestmark = [pytest.mark.no_postgres, pytest.mark.allow_missing_rule_binding]
SHA = "a" * 40
SCOPE = {"tenant_id": 1, "task_ids": ["task"], "action_ids": ["old-action"],
    "expected_count": 1, "deployed_sha": SHA}
OPERATION = BacklogReplanOperation("QA", "QA-backlog-approval", SHA)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        seed_backlog(current)
        yield current
    engine.dispose()


def seed_backlog(session):
    session.add(Tenant(id=1, name="QA"))
    session.flush()
    session.add_all([Task(id="task", tenant_id=1, name="QA", type="channel_like", status="running",
        fulfillment_contract_version="fact_first_v3", task_lifecycle_epoch=1),
        TgAccount(id=1, tenant_id=1, display_name="QA", phone_masked="QA"),
        OperationTarget(id=1, tenant_id=1, target_type="channel", tg_peer_id="-1001", title="QA")])
    session.flush()
    session.add(ChannelMessage(id=1, tenant_id=1, channel_target_id=1, message_id=1))
    session.flush()
    action = Action(id="old-action", tenant_id=1, task_id="task", task_type="channel_like",
        action_type="like_message", account_id=1, status="pending", task_lifecycle_epoch=1,
        scheduled_at=_now(), pacing_slot_key="QA-slot", payload={"channel_message_id": 1,
            "reaction_fulfillment_obligation_id": "obligation", "channel_id": "-1001", "message_id": 1})
    session.add(action)
    session.flush()
    session.add_all([ReactionFulfillmentObligation(id="obligation", tenant_id=1, task_id="task",
        channel_message_id=1, account_id=1, reaction_contract_version=1, current_action_id=action.id,
        action_attempt_no=1, status="pending", task_lifecycle_epoch=1),
        AccountPacingReservation(id="reservation", tenant_id=1, task_id="task", account_id=1,
            pacing_slot_key="QA-slot", policy_version="QA", due_at=_now(), release_not_before_at=_now(),
            effective_claim_at=_now(), source_deadline_at=_now() + timedelta(days=1),
            action_id=action.id, state="cancelled", version=3)])
    session.commit()


@pytest.mark.parametrize("expired", [False, True])
def test_exact_retirement_preserves_task_and_timeline_and_is_idempotent(session, expired):
    reservation = session.get(AccountPacingReservation, "reservation")
    if expired:
        reservation.source_deadline_at = _now() - timedelta(days=1)
        session.commit()
    original_timeline = timeline_hash(reservation)
    preview = preview_reaction_backlog(session, SCOPE)
    assert reservation.state == "cancelled"
    receipt = apply_reaction_backlog(session, preview, OPERATION)
    session.commit()
    assert timeline_hash(reservation) == original_timeline
    assert session.get(Action, "old-action").status == "skipped"
    obligation = session.get(ReactionFulfillmentObligation, "obligation")
    assert obligation.status == "open" and obligation.current_action_id is None
    assert reservation.state == ("missed" if expired else "reserved")
    assert session.get(Task, "task").status == "running"
    assert session.scalar(select(FulfillmentRemoteFact)).fact_kind == "safely_not_executed"
    assert apply_reaction_backlog(session, preview, OPERATION) == receipt
    assert session.scalar(select(func.count()).select_from(AuditLog)) == 1
    with Session(session.get_bind()) as readback:
        assert verify_reaction_backlog(readback, receipt)["persistence_status"] == "persisted_verified"


def test_old_receipt_cannot_change_replacement_action(session):
    preview = preview_reaction_backlog(session, SCOPE)
    receipt = apply_reaction_backlog(session, preview, OPERATION)
    replacement = Action(id="replacement", tenant_id=1, task_id="task", task_type="channel_like",
        action_type="like_message", status="pending", account_id=1, scheduled_at=_now(),
        payload=dict(session.get(Action, "old-action").payload), pacing_slot_key="QA-slot")
    session.add(replacement)
    session.flush()
    assert ensure_action_obligation(session, replacement)
    obligation = session.get(ReactionFulfillmentObligation, "obligation")
    bind_obligation_action(obligation, replacement)
    bind_account_pacing_reservation(session.get(AccountPacingReservation, "reservation"), replacement)
    session.commit()
    assert apply_reaction_backlog(session, preview, OPERATION) == receipt
    assert obligation.current_action_id == "replacement"
    assert verify_reaction_backlog(session, receipt)["replacement_bound_count"] == 1


def test_normal_materializer_creates_new_id_even_when_original_due_is_unchanged(session):
    task, old = session.get(Task, "task"), session.get(Action, "old-action")
    payload = LikeMessagePayload.model_validate(old.payload)
    due = old.scheduled_at
    old.action_dedupe_key = _action_dedupe_key(task, _plan_batch_key(task, due),
        "like_message", 1, payload.model_dump(mode="json"))
    session.commit()
    preview = preview_reaction_backlog(session, SCOPE)
    apply_reaction_backlog(session, preview, OPERATION)
    replacement = create_like_action(session, task, 1, due, payload)
    assert replacement.id != old.id
    assert replacement.status == "pending" and old.status == "skipped"


@pytest.mark.parametrize("drift", ["action", "reservation", "task", "epoch", "owner", "attempt", "unknown"])
def test_changed_preview_or_remote_evidence_never_applies(session, drift):
    preview = preview_reaction_backlog(session, SCOPE)
    action = session.get(Action, "old-action")
    if drift == "action":
        action.action_version += 1
    if drift == "reservation":
        session.get(AccountPacingReservation, "reservation").version += 1
    if drift == "task":
        session.get(Task, "task").type_config = {"changed": True}
    if drift == "epoch":
        action.task_lifecycle_epoch += 1
    if drift == "owner":
        action.claim_owner = "new-owner"
    if drift == "attempt":
        session.add(ExecutionAttempt(tenant_id=1, action_id=action.id, status="before_call"))
    if drift == "unknown":
        action.result = {"error_code": "remote_outcome_unknown"}
    session.commit()
    with pytest.raises(ValueError, match="reaction_backlog"):
        apply_reaction_backlog(session, preview, OPERATION)
    session.rollback()
    assert action.status == "pending"
    assert session.get(AccountPacingReservation, "reservation").state == "cancelled"
    assert session.scalar(select(func.count()).select_from(AuditLog)) == 0


@pytest.mark.parametrize("field,value", [("tenant_id", 2), ("expected_count", 2), ("action_ids", ["missing"]),
    ("deployed_sha", "invalid")])
def test_invalid_scope_is_rejected_without_writes(session, field, value):
    with pytest.raises(ValueError, match="reaction_backlog"):
        preview_reaction_backlog(session, {**SCOPE, field: value})
    assert session.get(Action, "old-action").status == "pending"


@pytest.mark.parametrize("kind", ["journal", "fact", "reaction"])
def test_any_durable_remote_evidence_excludes_replan(session, kind):
    if kind == "journal":
        session.add(GatewayRequestEvidenceJournal(tenant_id=1, action_id="old-action",
            execution_attempt_id="historical", gateway_request_identity="QA", request_fingerprint="QA",
            target_fingerprint="QA", result_fingerprint="QA", evidence_hash="QA"))
    if kind == "fact":
        session.add(FulfillmentRemoteFact(tenant_id=1, task_type="channel_like", task_id="task",
            obligation_type="reaction", obligation_id="obligation", action_id="old-action",
            attempt_id="historical", mutation_kind="like_message", remote_mutation_key_hash="QA",
            gateway_request_hash="QA", fact_kind="remote_outcome_unknown", fact_identity_hash="QA"))
    if kind == "reaction":
        session.add(ReactionRemoteFact(tenant_id=1, obligation_id="obligation", target_peer_id="-1001",
            channel_message_id=1, account_id=1, reaction_state_revision="QA",
            reaction_evidence_hash="QA", remote_confirmed_at=_now()))
    session.commit()
    with pytest.raises(ValueError, match="reaction_backlog"):
        preview_reaction_backlog(session, SCOPE)
    assert session.get(AccountPacingReservation, "reservation").state == "cancelled"


@pytest.mark.parametrize("change", ["sha", "hash", "audit"])
def test_operation_integrity_required(session, change):
    preview = preview_reaction_backlog(session, SCOPE)
    operation = OPERATION
    if change == "sha":
        operation = BacklogReplanOperation("QA", "QA", "b" * 40)
    if change == "hash":
        preview = {**preview, "state_hash": "b" * 64}
    if change == "audit":
        operation = BacklogReplanOperation("", "", SHA)
    with pytest.raises(ValueError, match="reaction_backlog"):
        apply_reaction_backlog(session, preview, operation)
    assert session.get(Action, "old-action").status == "pending"
