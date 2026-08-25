from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import (
    AuditLog,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
)
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc import start_next_online_abc_item
import app.services.authorization_dr.online_abc_completed_checkpoint_pause as checkpoint
from app.services.authorization_dr.online_abc_release_rebind import (
    apply_execution_release_rebind,
    preview_execution_release_rebind,
)
from tests import test_authorization_online_abc as abc_tests


pytestmark = pytest.mark.no_postgres
NEW_RELEASE_SHA = "b" * 40
CHECKPOINT_KEY = "abc-completed-checkpoint:test:101:v1"
INTERRUPTION_REF = "ssh:missing-runner-result:test"


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


def test_apply_projects_completed_item_and_creates_release_pause(db_session, monkeypatch) -> None:
    batch_id, account_id = _checkpoint(db_session, monkeypatch)
    before = _authorization_snapshot(db_session, account_id)

    preview = _preview(db_session, batch_id, account_id)
    result = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])

    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = _item(db_session, batch_id, account_id)
    slots = _slots(db_session, item.id)
    assert preview["e4_remote_message_id"] == f"test-{account_id}"
    assert result["already_applied"] is False
    assert batch.status == "stopped"
    assert batch.execution_release_sha == abc_tests.RELEASE_SHA
    assert item.status == item.outcome == "succeeded"
    assert item.primary_probe_outcome == "succeeded"
    assert {slot.outcome for slot in slots.values()} == {"succeeded"}
    assert all(slot.operation_id for slot in slots.values())
    assert _authorization_snapshot(db_session, account_id) == before
    assert _latest_batch_audit(db_session, batch_id).action == "生产版本变化暂停 ABC runner"


def test_existing_release_rebind_accepts_completed_checkpoint_pause(db_session, monkeypatch) -> None:
    batch_id, account_id = _checkpoint(db_session, monkeypatch)
    preview = _preview(db_session, batch_id, account_id)
    _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])

    rebind = preview_execution_release_rebind(
        db_session, batch_id, runtime_release_sha=NEW_RELEASE_SHA,
        requested_by="requester", approved_by="approver", approval_ref="ABC-10",
    )
    result = apply_execution_release_rebind(
        db_session, batch_id, runtime_release_sha=NEW_RELEASE_SHA,
        expected_fingerprint=rebind["fingerprint"], requested_by="requester",
        approved_by="approver", approval_ref="ABC-10",
    )

    assert rebind["succeeded_count"] == 1
    assert rebind["pending_count"] == 9
    assert result["execution_release_sha"] == NEW_RELEASE_SHA
    assert result["batch_status"] == "running"


def test_apply_and_readback_are_idempotent(db_session, monkeypatch) -> None:
    batch_id, account_id = _checkpoint(db_session, monkeypatch)
    preview = _preview(db_session, batch_id, account_id)

    first = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    second = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    readback = checkpoint.readback_completed_checkpoint_pause(
        db_session, batch_id, account_id, idempotency_key=CHECKPOINT_KEY,
    )

    assert second["already_applied"] is True
    assert readback == second
    assert first["batch_version"] == second["batch_version"]


def test_apply_rejects_changed_item_version(db_session, monkeypatch) -> None:
    batch_id, account_id = _checkpoint(db_session, monkeypatch)
    preview = _preview(db_session, batch_id, account_id)
    item = _item(db_session, batch_id, account_id)
    item.version += 1
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])

    assert exc_info.value.code == "migration_fingerprint_conflict"
    assert item.status == "running"


def test_preview_rejects_missing_remote_message_id(db_session, monkeypatch) -> None:
    batch_id, account_id = _checkpoint(db_session, monkeypatch)
    operation = _operations(db_session, account_id)["e4"]
    audit_row = db_session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_dr_operation",
        AuditLog.target_id == operation.id,
        AuditLog.action == "完成 ABC canary E4",
    ))
    audit_row.detail = "approval_ref=ABC-10"
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)

    assert exc_info.value.code == "online_abc_completed_checkpoint_operation_invalid"


def test_preview_rejects_primary_drift(db_session, monkeypatch) -> None:
    batch_id, account_id = _checkpoint(db_session, monkeypatch)
    account = db_session.get(TgAccount, account_id)
    account.connection_generation += 1
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)

    assert exc_info.value.code == "online_abc_primary_drift"


def test_preview_rejects_global_sensitive_operation(db_session, monkeypatch) -> None:
    batch_id, account_id = _checkpoint(db_session, monkeypatch)
    abc_tests._add_operation(
        db_session, abc_tests.ACCOUNT_IDS[1], "completed-checkpoint-sensitive", "pending",
    )

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)

    assert exc_info.value.code == "online_abc_completed_checkpoint_runtime_active"


def test_preview_rejects_same_runtime_release(db_session, monkeypatch) -> None:
    batch_id, account_id = _checkpoint(db_session, monkeypatch)

    with pytest.raises(AuthorizationDrError) as exc_info:
        checkpoint.preview_completed_checkpoint_pause(
            db_session, batch_id, account_id, runtime_release_sha=abc_tests.RELEASE_SHA,
            idempotency_key=CHECKPOINT_KEY, requested_by="requester",
            approved_by="approver", approval_ref="ABC-10",
            interruption_ref=INTERRUPTION_REF,
        )

    assert exc_info.value.code == "runtime_image_mismatch"


def _checkpoint(session, monkeypatch) -> tuple[str, int]:
    batch_id = abc_tests._apply(session, abc_tests._preview(session)["fingerprint"])["batch_id"]
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    batch.selection_mode = "all_online_accounts"
    command = start_next_online_abc_item(
        session, batch_id, actor="approver", approval_ref="ABC-10",
    )
    abc_tests._qualify_primary(session, command["account_id"])
    abc_tests._add_operations(session, command, status="succeeded")
    monkeypatch.setattr(checkpoint, "preview_abc_e4", _artifact_preview)
    session.commit()
    return batch_id, command["account_id"]


def _preview(session, batch_id: str, account_id: int) -> dict:
    return checkpoint.preview_completed_checkpoint_pause(
        session, batch_id, account_id, runtime_release_sha=NEW_RELEASE_SHA,
        idempotency_key=CHECKPOINT_KEY, requested_by="requester",
        approved_by="approver", approval_ref="ABC-10",
        interruption_ref=INTERRUPTION_REF,
    )


def _apply(session, batch_id: str, account_id: int, *, fingerprint: str) -> dict:
    return checkpoint.apply_completed_checkpoint_pause(
        session, batch_id, account_id, runtime_release_sha=NEW_RELEASE_SHA,
        idempotency_key=CHECKPOINT_KEY, expected_fingerprint=fingerprint,
        requested_by="requester", approved_by="approver", approval_ref="ABC-10",
        interruption_ref=INTERRUPTION_REF,
    )


def _artifact_preview(_session, tenant_id: int, account_id: int, *, idempotency_key: str) -> dict:
    return {
        "tenant_id": tenant_id,
        "account_id": account_id,
        "idempotency_key": idempotency_key,
        "primary": [1101, 1, "primary-auth-key"],
        "standby_1": [2101, 1, "standby-auth-key"],
        "standby_2": [3101, 1, "malaysia-auth-key"],
        "bundle": ["bundle-101", 1, 2, "probe-101"],
        "runtime": [1, 1, "off", "my-node", 1, "c" * 40],
        "fingerprint": "e" * 64,
    }


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


def _operations(session, account_id: int) -> dict:
    rows = list(session.scalars(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.account_id == account_id,
    ).order_by(TgAuthorizationDrOperation.created_at, TgAuthorizationDrOperation.id)))
    return {"b": rows[0], "c": rows[1], "e4": rows[2]}


def _authorization_snapshot(session, account_id: int) -> list[tuple]:
    rows = session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account_id,
    ).order_by(TgAccountAuthorization.id))
    return [
        (row.id, row.status, row.health_status, row.is_current, row.is_slot_current,
         row.session_ciphertext, row.fact_version)
        for row in rows
    ]


def _latest_batch_audit(session, batch_id: str) -> AuditLog:
    return session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch_id,
    ).order_by(AuditLog.id.desc()).limit(1))
