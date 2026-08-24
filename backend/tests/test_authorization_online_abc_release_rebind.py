from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import (
    AuditLog,
    AuthorizationDrExecutionNode,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
)
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc import start_next_online_abc_item, sync_online_abc_batch
from app.services.authorization_dr.online_abc_operations import online_abc_item_operations
from app.services.authorization_dr.online_abc_release_rebind import (
    apply_execution_release_rebind,
    preview_execution_release_rebind,
)
from tests import test_authorization_online_abc as abc_tests


pytestmark = pytest.mark.no_postgres
NEW_RELEASE_SHA = "b" * 40


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


def test_preview_and_apply_rebind_only_batch_execution_release(db_session) -> None:
    batch_id = _paused_batch(db_session)
    before = _authorization_snapshot(db_session, batch_id)

    preview = _preview(db_session, batch_id)
    result = _apply(db_session, batch_id, preview["fingerprint"])

    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    assert preview["succeeded_count"] == 1
    assert preview["pending_count"] == 9
    assert preview["previous_execution_release_sha"] == abc_tests.RELEASE_SHA
    assert result["already_applied"] is False
    assert batch.status == "running"
    assert batch.execution_release_sha == NEW_RELEASE_SHA
    assert batch.deployed_release_sha == abc_tests.RELEASE_SHA
    assert _authorization_snapshot(db_session, batch_id) == before


def test_apply_is_idempotent_by_fingerprint(db_session) -> None:
    batch_id = _paused_batch(db_session)
    preview = _preview(db_session, batch_id)

    first = _apply(db_session, batch_id, preview["fingerprint"])
    second = _apply(db_session, batch_id, preview["fingerprint"])

    assert first["batch_version"] == second["batch_version"]
    assert second["already_applied"] is True


def test_apply_rejects_changed_preview(db_session) -> None:
    batch_id = _paused_batch(db_session)
    preview = _preview(db_session, batch_id)
    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    batch.version += 1
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _apply(db_session, batch_id, preview["fingerprint"])

    assert exc_info.value.code == "migration_fingerprint_conflict"
    assert batch.execution_release_sha == abc_tests.RELEASE_SHA


def test_preview_rejects_completed_primary_drift(db_session) -> None:
    batch_id = _paused_batch(db_session)
    item = _succeeded_item(db_session, batch_id)
    account = db_session.get(TgAccount, item.account_id)
    account.authorization_generation += 1
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id)

    assert exc_info.value.code == "online_abc_primary_drift"


def test_preview_rejects_incomplete_completed_operation(db_session) -> None:
    batch_id = _paused_batch(db_session)
    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = _succeeded_item(db_session, batch_id)
    operation = online_abc_item_operations(db_session, batch, item)["e4"]
    operation.status = "failed"
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id)

    assert exc_info.value.code == "online_abc_release_rebind_completed_incomplete"


def test_preview_rejects_global_unknown(db_session) -> None:
    batch_id = _paused_batch(db_session)
    abc_tests._add_operation(
        db_session,
        abc_tests.ACCOUNT_IDS[1],
        "release-rebind-global-unknown",
        "reconcile_unknown",
    )

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id)

    assert exc_info.value.code == "global_reconcile_unknown"


def test_preview_rejects_active_sensitive_operation(db_session) -> None:
    batch_id = _paused_batch(db_session)
    abc_tests._add_operation(
        db_session,
        abc_tests.ACCOUNT_IDS[1],
        "release-rebind-global-sensitive",
        "pending",
    )

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id)

    assert exc_info.value.code == "online_abc_sensitive_operation"


def test_preview_rejects_malaysia_active_client(db_session) -> None:
    batch_id = _paused_batch(db_session)
    node = db_session.scalar(select(AuthorizationDrExecutionNode).where(
        AuthorizationDrExecutionNode.region_code == "my",
    ))
    if node is None:
        node = AuthorizationDrExecutionNode(
            id="my-test-node",
            region_code="my",
            purpose="standby_authorization",
            capability_version="test",
            runtime_image_sha="d" * 40,
            standby_egress_id="my-egress",
            status="ready",
            active_client_count=1,
        )
        db_session.add(node)
    else:
        node.active_client_count = 1
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id)

    assert exc_info.value.code == "malaysia_client_leak"


def test_preview_rejects_unproven_pause_reason(db_session) -> None:
    batch_id = _paused_batch(db_session)
    db_session.add(AuditLog(
        tenant_id=1,
        actor="approver",
        action="unrelated batch action",
        target_type="tg_authorization_online_abc_batches",
        target_id=batch_id,
        detail="approval_ref=ABC-10",
    ))
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id)

    assert exc_info.value.code == "online_abc_release_rebind_pause_unproven"


def _paused_batch(session) -> str:
    batch_id = abc_tests._apply(session, abc_tests._preview(session)["fingerprint"])["batch_id"]
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-10")
    abc_tests._qualify_primary(session, command["account_id"])
    abc_tests._add_operations(session, command, status="succeeded")
    sync_online_abc_batch(session, batch_id, actor="approver", approval_ref="ABC-10")
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    batch.status = "stopped"
    batch.version += 1
    session.add(AuditLog(
        tenant_id=1,
        actor="approver",
        action="生产版本变化暂停 ABC runner",
        target_type="tg_authorization_online_abc_batches",
        target_id=batch_id,
        detail=(
            "approval_ref=ABC-10; blocker=production_release_changed_mid_chunk; "
            "succeeded=1; pending=9; running_items=0; global_unknown=0; runtime=off"
        ),
    ))
    session.commit()
    return batch_id


def _preview(session, batch_id: str) -> dict:
    return preview_execution_release_rebind(
        session,
        batch_id,
        runtime_release_sha=NEW_RELEASE_SHA,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
    )


def _apply(session, batch_id: str, fingerprint: str) -> dict:
    return apply_execution_release_rebind(
        session,
        batch_id,
        runtime_release_sha=NEW_RELEASE_SHA,
        expected_fingerprint=fingerprint,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
    )


def _succeeded_item(session, batch_id: str):
    return session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.status == "succeeded",
    ))


def _authorization_snapshot(session, batch_id: str) -> dict:
    item = _succeeded_item(session, batch_id)
    account = session.get(TgAccount, item.account_id)
    authorizations = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account.id,
    ).order_by(TgAccountAuthorization.id)))
    operations = list(session.scalars(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.account_id == account.id,
    ).order_by(TgAuthorizationDrOperation.id)))
    return {
        "account": (
            account.current_authorization_id,
            account.authorization_generation,
            account.authorization_fact_generation,
            account.connection_generation,
            account.session_ciphertext,
        ),
        "authorizations": [
            (row.id, row.is_current, row.is_slot_current, row.session_ciphertext, row.fact_version)
            for row in authorizations
        ],
        "item": (item.id, item.status, item.outcome, item.version),
        "operations": [(row.id, row.status, row.operation_version) for row in operations],
    }
