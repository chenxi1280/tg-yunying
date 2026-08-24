from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import (
    AuditLog,
    AuthorizationDrExecutionNode,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
)
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc_pending_plan_rebase import (
    apply_pending_plan_rebase,
    preview_pending_plan_rebase,
)
from tests import test_authorization_online_abc as abc_tests


pytestmark = pytest.mark.no_postgres


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


def test_pending_plan_rebase_changes_only_allowed_item_fields(db_session) -> None:
    batch, item = _stale_pending_item(db_session)
    protected = _protected_snapshots(db_session, batch, item)
    item_before = _row_snapshot(item)
    preview = _preview(db_session, batch.id)

    result = apply_pending_plan_rebase(
        db_session,
        batch.id,
        expected_target_count=1,
        idempotency_key="pending-plan-rebase-1",
        expected_fingerprint=preview["fingerprint"],
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
    )

    db_session.expire_all()
    item = db_session.get(TgAuthorizationOnlineAbcItem, item.id)
    changed = {
        key for key, value in item_before.items()
        if _row_snapshot(item)[key] != value
    }
    assert result["target_count"] == 1
    assert result["already_applied"] is False
    assert changed == {"app_b_assignment_version", "standby_1_plan", "version"}
    assert item.standby_1_plan == "provision"
    assert item.standby_2_plan == "migrate"
    assert _protected_snapshots(db_session, batch, item) == protected
    assert _audit_count(db_session, batch.id) == 1

    repeated = apply_pending_plan_rebase(
        db_session,
        batch.id,
        expected_target_count=1,
        idempotency_key="pending-plan-rebase-1",
        expected_fingerprint=preview["fingerprint"],
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
    )
    assert repeated["already_applied"] is True
    assert _audit_count(db_session, batch.id) == 1


def test_pending_plan_rebase_rejects_item_version_drift(db_session) -> None:
    batch, item = _stale_pending_item(db_session)
    preview = _preview(db_session, batch.id)
    item.version += 1
    db_session.commit()
    before = _row_snapshot(item)

    with pytest.raises(AuthorizationDrError) as exc_info:
        apply_pending_plan_rebase(
            db_session,
            batch.id,
            expected_target_count=1,
            idempotency_key="pending-plan-rebase-1",
            expected_fingerprint=preview["fingerprint"],
            requested_by="requester",
            approved_by="approver",
            approval_ref="ABC-10",
        )

    assert exc_info.value.code == "migration_fingerprint_conflict"
    assert _row_snapshot(item) == before
    assert _audit_count(db_session, batch.id) == 0


def test_pending_plan_rebase_rejects_any_existing_operation(db_session) -> None:
    batch, item = _stale_pending_item(db_session)
    keys = abc_tests.runner.online_abc_operation_keys(batch, item)
    abc_tests._add_operation(db_session, item.account_id, keys["b"], "succeeded")

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch.id)

    assert exc_info.value.code == "online_abc_pending_rebase_target_count"
    assert item.standby_1_plan == "blocked"
    assert _audit_count(db_session, batch.id) == 0


def test_pending_plan_rebase_rejects_active_my_client(db_session) -> None:
    batch, item = _stale_pending_item(db_session)
    db_session.add(AuthorizationDrExecutionNode(
        id="my-node",
        region_code="my",
        purpose="authorization_dr",
        capability_version="test",
        standby_egress_id="standby_my:test",
        status="ready",
        active_client_count=1,
    ))
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch.id)

    assert exc_info.value.code == "malaysia_client_leak"
    assert item.standby_1_plan == "blocked"
    assert _audit_count(db_session, batch.id) == 0


def test_pending_plan_rebase_rejects_non_quiescent_batch(db_session) -> None:
    batch, item = _stale_pending_item(db_session)
    other = db_session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
        TgAuthorizationOnlineAbcItem.id != item.id,
    ).limit(1))
    other.status = "running"
    other.outcome = "running"
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch.id)

    assert exc_info.value.code == "online_abc_pending_rebase_item_active"
    assert item.standby_1_plan == "blocked"
    assert _audit_count(db_session, batch.id) == 0


def test_pending_plan_rebase_rejects_unsafe_idempotency_key(db_session) -> None:
    batch, item = _stale_pending_item(db_session)

    with pytest.raises(AuthorizationDrError) as exc_info:
        preview_pending_plan_rebase(
            db_session,
            batch.id,
            expected_target_count=1,
            idempotency_key="unsafe; fingerprint=injected",
            requested_by="requester",
            approved_by="approver",
            approval_ref="ABC-10",
        )

    assert exc_info.value.code == "idempotency_key_required"
    assert item.standby_1_plan == "blocked"
    assert _audit_count(db_session, batch.id) == 0


def _stale_pending_item(session):
    batch_id = abc_tests._apply(session, abc_tests._preview(session)["fingerprint"])["batch_id"]
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
    ).order_by(TgAuthorizationOnlineAbcItem.ordinal).limit(1))
    batch.status = "stopped"
    item.standby_1_plan = "blocked"
    item.app_b_assignment_version = 0
    session.commit()
    return batch, item


def _preview(session, batch_id: str) -> dict:
    return preview_pending_plan_rebase(
        session,
        batch_id,
        expected_target_count=1,
        idempotency_key="pending-plan-rebase-1",
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
    )


def _protected_snapshots(session, batch, item) -> dict:
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    source_c = session.get(TgAccountAuthorization, item.source_c_authorization_id)
    slots = list(session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item.id,
    ).order_by(TgAuthorizationOnlineAbcSlotResult.logical_slot)))
    return {
        "batch": _row_snapshot(session.get(TgAuthorizationOnlineAbcBatch, batch.id)),
        "account": _row_snapshot(account),
        "primary": _row_snapshot(primary),
        "source_c": _row_snapshot(source_c),
        "slots": [_row_snapshot(slot) for slot in slots],
    }


def _row_snapshot(row) -> dict:
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


def _audit_count(session, batch_id: str) -> int:
    return len(list(session.scalars(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_batches",
        AuditLog.target_id == batch_id,
        AuditLog.action == "重基线 pending ABC B/C plan",
    ))))
