from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import (
    AuditLog,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationDrReconcileCase,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
    TgLoginFlow,
)
from app.services._common import _now
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc import start_next_online_abc_item
from app.services.authorization_dr.online_abc_manifest import (
    apply_full_online_abc_batch,
    preview_full_online_abc_batch,
)
from app.services.authorization_dr.online_abc_release_interrupted import (
    ACTION,
    BLOCKER,
    CLASSIFICATION,
    apply_release_interrupted_b,
    preview_release_interrupted_b,
    readback_release_interrupted_b,
)
from tests import test_authorization_online_abc as abc_tests


pytestmark = pytest.mark.no_postgres
NEW_RELEASE_SHA = "b" * 40
INTERRUPTION_KEY = "abc-release-interrupted:test:101:v1"
INTERRUPTION_REF = "deploy-run:123:new-release"
APPROVAL_REF = "user-approved-release-interrupted"


@pytest.fixture
def db_session():
    fixture = abc_tests.session.__wrapped__()
    session = next(fixture)
    try:
        yield session
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass


def test_preview_is_read_only_and_freezes_exact_pre_flow_boundary(db_session) -> None:
    batch_id, account_id, operation_id = _interrupted_item(db_session)
    before = _snapshot(db_session, batch_id, account_id)

    preview = _preview(db_session, batch_id, account_id)

    assert preview["operation_id"] == operation_id
    assert preview["operation_status"] == "login_remote_started"
    assert preview["remote_call_state"] == "started"
    assert preview["previous_execution_release_sha"] == abc_tests.RELEASE_SHA
    assert preview["runtime_release_sha"] == NEW_RELEASE_SHA
    assert preview["classification"] == CLASSIFICATION
    assert preview["primary"]["state"] == "legacy_frozen"
    assert preview["interrupted_flow"]["status"] == "intent_persisted"
    assert preview["interrupted_flow"]["challenge_sent"] is False
    assert _snapshot(db_session, batch_id, account_id) == before
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_apply_records_manual_debt_and_preserves_remote_effect_and_a(db_session) -> None:
    batch_id, account_id, operation_id = _interrupted_item(db_session)
    before_a = _a_snapshot(db_session, account_id)
    before_effect = db_session.get(TgAuthorizationDrOperation, operation_id).remote_effect_started_at
    preview = _preview(db_session, batch_id, account_id)

    result = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])

    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = _item(db_session, batch_id, account_id)
    slots = _slots(db_session, item.id)
    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    flow = _flow(db_session, account_id)
    case = db_session.get(TgAuthorizationDrReconcileCase, operation.reconcile_case_id)
    assert result["already_applied"] is False
    assert batch.status == "running" and batch.execution_release_sha == NEW_RELEASE_SHA
    assert item.status == item.outcome == "manual_required"
    assert slots["standby_1"].outcome == slots["standby_2"].outcome == "manual_required"
    assert slots["standby_1"].operation_id == operation.id
    assert slots["standby_2"].operation_id is None
    assert (operation.status, operation.remote_call_state) == ("manual_required", "reconciled_hold")
    assert operation.login_flow_id == flow.id
    assert operation.remote_effect_started_at == before_effect
    assert (flow.status, flow.flow_version, flow.failure_type) == ("superseded", 2, BLOCKER)
    assert flow.authorization_id is None and flow.challenge_sent_at is None
    assert flow.temporary_session_ciphertext is None and flow.phone_code_hash_ciphertext is None
    assert operation.blocker_code == BLOCKER and operation.finished_at
    assert case and case.status == "applied" and case.classification == CLASSIFICATION
    assert case.evidence_fingerprint == preview["fingerprint"]
    assert _a_snapshot(db_session, account_id) == before_a
    audit_row = db_session.scalar(select(AuditLog).where(AuditLog.action == ACTION))
    assert audit_row and INTERRUPTION_REF in audit_row.detail


def test_apply_is_idempotent_and_rejects_fingerprint_conflict(db_session) -> None:
    batch_id, account_id, _ = _interrupted_item(db_session)
    preview = _preview(db_session, batch_id, account_id)

    first = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    second = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    readback = readback_release_interrupted_b(
        db_session, batch_id, account_id, idempotency_key=INTERRUPTION_KEY,
    )

    assert second["already_applied"] is True
    assert readback == second
    assert readback["interruption_ref"] == INTERRUPTION_REF
    assert readback["remote_effect_started_at"] == preview["remote_effect_started_at"]
    assert readback["primary"] == preview["primary"]
    assert readback["interrupted_flow"]["status"] == "superseded"
    assert readback["interrupted_flow"]["id"] == preview["interrupted_flow"]["id"]
    assert second["item_version"] == first["item_version"]
    with pytest.raises(AuthorizationDrError) as exc_info:
        _apply(db_session, batch_id, account_id, fingerprint="f" * 64)
    assert exc_info.value.code == "idempotency_key_conflict"


def test_apply_rechecks_fingerprint_under_lock(db_session) -> None:
    batch_id, account_id, operation_id = _interrupted_item(db_session)
    preview = _preview(db_session, batch_id, account_id)
    db_session.get(TgAuthorizationDrOperation, operation_id).operation_version += 1
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])

    assert exc_info.value.code == "migration_fingerprint_conflict"
    assert not db_session.scalar(select(func.count()).select_from(TgAuthorizationDrReconcileCase))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("login_flow_id", 99),
        ("login_challenge_sent_at", _now()),
        ("login_code_message_id", "telegram-message"),
        ("login_code_received_at", _now()),
        ("candidate_authorization_id", 1),
        ("owner_node_id", "runner-node"),
        ("reconcile_status", "previewed"),
    ],
)
def test_preview_rejects_any_downstream_or_owner_fact(db_session, field, value) -> None:
    batch_id, account_id, operation_id = _interrupted_item(db_session)
    setattr(db_session.get(TgAuthorizationDrOperation, operation_id), field, value)
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)

    assert exc_info.value.code == "online_abc_release_interrupted_state_invalid"


def test_preview_rejects_same_release_a_drift_or_second_sensitive_operation(db_session) -> None:
    batch_id, account_id, _ = _interrupted_item(db_session)
    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id, release_sha=abc_tests.RELEASE_SHA)
    assert exc_info.value.code == "online_abc_release_interrupted_batch_invalid"

    db_session.get(TgAccount, account_id).connection_generation += 1
    db_session.commit()
    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)
    assert exc_info.value.code == "online_abc_primary_drift"

    db_session.get(TgAccount, account_id).connection_generation -= 1
    abc_tests._add_operation(db_session, abc_tests.ACCOUNT_IDS[1], "second-sensitive", "approved")
    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)
    assert exc_info.value.code == "online_abc_release_interrupted_runtime_active"


def test_preview_rejects_second_ambiguous_intent_flow(db_session) -> None:
    batch_id, account_id, operation_id = _interrupted_item(db_session)
    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    db_session.add(TgLoginFlow(
        tenant_id=operation.tenant_id,
        account_id=account_id,
        method="code",
        status="intent_persisted",
        authorization_role="standby_1",
        developer_app_id=operation.developer_app_id,
        created_at=operation.remote_effect_started_at,
    ))
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)

    assert exc_info.value.code == "online_abc_release_interrupted_state_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "等待验证码"),
        ("challenge_sent_at", _now()),
        ("temporary_session_ciphertext", "encrypted-session"),
        ("phone_code_hash_ciphertext", "encrypted-hash"),
        ("code_preview", "12345"),
        ("qr_payload", "qr-payload"),
        ("authorization_id", 1),
        ("superseded_by_flow_id", 99),
        ("failure_detail", "downstream-detail"),
        ("remote_error_type", "rpc_error"),
    ],
)
def test_preview_rejects_intent_flow_with_any_remote_or_terminal_fact(
    db_session, field, value,
) -> None:
    batch_id, account_id, _ = _interrupted_item(db_session)
    setattr(_flow(db_session, account_id), field, value)
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)

    assert exc_info.value.code == "online_abc_release_interrupted_state_invalid"


def test_no_flow_variant_remains_supported(db_session) -> None:
    batch_id, account_id, _ = _interrupted_item(db_session, include_flow=False)

    preview = _preview(db_session, batch_id, account_id)
    result = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])

    assert preview["interrupted_flow"] is None
    assert result["interrupted_flow"] is None
    assert result["item_outcome"] == "manual_required"


def _interrupted_item(session, *, include_flow: bool = True) -> tuple[str, int, str]:
    batch_id = _new_full_batch(session)
    command = start_next_online_abc_item(
        session, batch_id, actor="approver", approval_ref="ABC-FULL",
    )
    account = session.get(TgAccount, command["account_id"])
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    primary.health_status = "legacy"
    operation = abc_tests._add_operation(
        session, command["account_id"], command["b_idempotency_key"], "login_remote_started",
    )
    operation.source_authorization_id = primary.id
    operation.code_source_authorization_id = primary.id
    operation.expected_current_authorization_id = primary.id
    operation.expected_authorization_generation = account.authorization_generation
    operation.expected_authorization_fact_generation = account.authorization_fact_generation
    operation.expected_connection_generation = account.connection_generation
    operation.expected_code_source_fact_version = primary.fact_version
    operation.remote_call_state = "started"
    operation.remote_effect_started_at = _now()
    operation.operation_version = 2
    operation.blocker_code = ""
    operation.reconcile_status = "none"
    if include_flow:
        _add_empty_intent(session, operation)
    session.commit()
    return batch_id, command["account_id"], operation.id


def _add_empty_intent(session, operation) -> None:
    session.add(TgLoginFlow(
        tenant_id=operation.tenant_id,
        account_id=operation.account_id,
        method="code",
        status="intent_persisted",
        authorization_role="standby_1",
        developer_app_id=operation.developer_app_id,
        created_at=_now(),
    ))


def _flow(session, account_id: int):
    return session.scalar(select(TgLoginFlow).where(
        TgLoginFlow.account_id == account_id,
        TgLoginFlow.authorization_role == "standby_1",
    ).order_by(TgLoginFlow.created_at.desc(), TgLoginFlow.id.desc()).limit(1))


def _new_full_batch(session) -> str:
    abc_tests._seed_accepted_canary(session)
    preview = preview_full_online_abc_batch(
        session, 1, idempotency_key="full-release-interrupted", deployed_release_sha=abc_tests.RELEASE_SHA,
    )
    return apply_full_online_abc_batch(
        session,
        1,
        idempotency_key="full-release-interrupted",
        deployed_release_sha=abc_tests.RELEASE_SHA,
        expected_fingerprint=preview["fingerprint"],
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-FULL",
    )["batch_id"]


def _preview(session, batch_id: str, account_id: int, *, release_sha: str = NEW_RELEASE_SHA) -> dict:
    return preview_release_interrupted_b(
        session,
        batch_id,
        account_id,
        runtime_release_sha=release_sha,
        idempotency_key=INTERRUPTION_KEY,
        requested_by="requester",
        approved_by="approver",
        approval_ref=APPROVAL_REF,
        interruption_ref=INTERRUPTION_REF,
    )


def _apply(session, batch_id: str, account_id: int, *, fingerprint: str) -> dict:
    return apply_release_interrupted_b(
        session,
        batch_id,
        account_id,
        runtime_release_sha=NEW_RELEASE_SHA,
        idempotency_key=INTERRUPTION_KEY,
        expected_fingerprint=fingerprint,
        requested_by="requester",
        approved_by="approver",
        approval_ref=APPROVAL_REF,
        interruption_ref=INTERRUPTION_REF,
    )


def _a_snapshot(session, account_id: int) -> tuple:
    account = session.get(TgAccount, account_id)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    return (
        account.current_authorization_id,
        account.session_ciphertext,
        account.authorization_generation,
        account.authorization_fact_generation,
        account.connection_generation,
        primary.session_ciphertext,
        primary.fact_version,
        primary.status,
        primary.health_status,
    )


def _snapshot(session, batch_id: str, account_id: int) -> tuple:
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = _item(session, batch_id, account_id)
    slots = _slots(session, item.id)
    operation = session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.account_id == account_id,
        TgAuthorizationDrOperation.status == "login_remote_started",
    ))
    flow = _flow(session, account_id)
    return (
        batch.status,
        batch.version,
        batch.execution_release_sha,
        item.status,
        item.outcome,
        item.version,
        slots["standby_1"].outcome,
        slots["standby_1"].version,
        slots["standby_2"].outcome,
        slots["standby_2"].version,
        operation.status,
        operation.operation_version,
        flow.status if flow else None,
        flow.flow_version if flow else None,
        flow.failure_type if flow else None,
        _a_snapshot(session, account_id),
    )


def _item(session, batch_id: str, account_id: int):
    return session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))


def _slots(session, item_id: str) -> dict:
    rows = session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item_id,
    ))
    return {row.logical_slot: row for row in rows}
