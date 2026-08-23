from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.services.authorization_dr.c_orphan_recovery as recovery
import app.services.authorization_dr.online_abc_runner as runner
from app.integrations.telegram.contracts import AccountAuthorizationSnapshot
from app.models import (
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationDrStageFact,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
)
from app.security import encrypt_secret
from app.services._common import _now
from app.services.authorization_dr.online_abc import (
    start_next_online_abc_item,
    sync_online_abc_batch,
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


def test_orphan_revoke_reopens_same_item_with_retry_key(db_session, monkeypatch) -> None:
    batch, item, operation = _stopped_unknown_item(db_session)
    remote = _remote_rows(operation)
    calls = {"list": 0, "cleanup": []}

    def list_authorizations(*_args, **_kwargs):
        calls["list"] += 1
        return remote if calls["list"] <= 2 else [remote[0], remote[1]]

    def cleanup(_session, authorization_hash, _credentials):
        calls["cleanup"].append(authorization_hash)
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(recovery.gateway, "list_authorizations", list_authorizations)
    monkeypatch.setattr(recovery.gateway, "cleanup_authorization", cleanup)
    preview = recovery.preview_c_orphan_recovery(db_session, batch.id, item.account_id)

    assert "candidate_authorization_hash" not in preview
    result = recovery.apply_c_orphan_recovery(
        db_session, batch.id, item.account_id,
        expected_fingerprint=preview["fingerprint"], requested_by="requester",
        approved_by="approver", approval_ref="ABC-ORPHAN",
    )

    assert calls["cleanup"] == ["new-c-hash"]
    assert result["operation_status"] == "migration_rolled_back_forward"
    db_session.expire_all()
    assert db_session.get(TgAuthorizationDrOperation, operation.id).remote_call_state == "compensated"
    assert db_session.get(TgAuthorizationOnlineAbcItem, item.id).blocker_code == recovery.RECOVERY_BLOCKER
    slot = db_session.scalar(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item.id,
        TgAuthorizationOnlineAbcSlotResult.logical_slot == "standby_2",
    ))
    assert slot.operation_id is None
    assert runner.next_online_abc_c_key(db_session, batch, item).endswith(":retry:1")

    resumed = runner.resume_online_abc_batch(
        db_session, batch.id, requested_by="requester", approved_by="approver",
        approval_ref="ABC-10", runtime_release_sha="b" * 40,
    )
    assert resumed["batch"]["status"] == "running"


def test_typed_duplicate_primary_uses_independent_healthy_b() -> None:
    primary = SimpleNamespace(
        id=1, status="active", health_status="invalid",
        last_authoritative_error_code="authorization_key_duplicated",
        telegram_user_id_digest="u", auth_key_fingerprint_digest="a",
        session_ciphertext="old",
    )
    standby = SimpleNamespace(
        id=2, health_status="healthy", telegram_user_id_digest="u",
        auth_key_fingerprint_digest="b", session_ciphertext="standby",
    )

    assert recovery._valid_revoker(primary, standby) is True


def _stopped_unknown_item(session):
    batch_id = abc_tests._apply(session, abc_tests._preview(session)["fingerprint"])["batch_id"]
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-10")
    item = session.get(TgAuthorizationOnlineAbcItem, command["item_id"])
    item.standby_1_plan = "already_qualified"
    b_slot = session.scalar(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item.id,
        TgAuthorizationOnlineAbcSlotResult.logical_slot == "standby_1",
    ))
    b_slot.outcome = "already_qualified"
    abc_tests._qualify_primary(session, item.account_id)
    _seed_known_hashes(session, item.account_id)
    created = abc_tests._add_c_operation(
        session, item.account_id, command["c_idempotency_key"], "provision_reconcile_unknown",
    )
    operation = session.get(TgAuthorizationDrOperation, created["operation_id"])
    operation.remote_call_state = "unknown"
    operation.developer_app_api_id_snapshot = 1003
    operation.remote_effect_started_at = _now()
    operation.login_code_received_at = _now()
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    operation.expected_code_source_user_id_digest = primary.telegram_user_id_digest
    operation.expected_code_source_auth_key_digest = primary.auth_key_fingerprint_digest
    account = session.get(TgAccount, item.account_id)
    account.authorization_fact_generation += 1
    primary.fact_version += 1
    session.add(TgAuthorizationDrStageFact(
        operation_id=operation.id, node_id="my-node-1", owner_epoch=1,
        stage="remote_login_confirmed", manifest_digest="a" * 64,
    ))
    session.commit()
    sync_online_abc_batch(session, batch_id, actor="approver", approval_ref="ABC-10")
    return session.get(TgAuthorizationOnlineAbcBatch, batch_id), item, operation


def _seed_known_hashes(session, account_id: int) -> None:
    account = session.get(TgAccount, account_id)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    source = session.get(TgAccountAuthorization, 2000 + account_id)
    primary.telegram_authorization_hash_ciphertext = encrypt_secret("primary-hash")
    source.telegram_authorization_hash_ciphertext = encrypt_secret("old-c-hash")
    session.commit()


def _remote_rows(operation):
    common = {
        "device_model": "PC", "platform": "Android", "system_version": "1",
        "app_name": "app", "app_version": "1", "country": "MY", "region": "",
        "date_active": _now(),
    }
    return [
        AccountAuthorizationSnapshot(
            authorization_hash="primary-hash", is_current=True, api_id=1002,
            date_created=_now() - timedelta(days=2), **common,
        ),
        AccountAuthorizationSnapshot(
            authorization_hash="old-c-hash", is_current=False, api_id=1003,
            date_created=_now() - timedelta(days=1), **common,
        ),
        AccountAuthorizationSnapshot(
            authorization_hash="new-c-hash", is_current=False, api_id=1003,
            date_created=operation.remote_effect_started_at + timedelta(seconds=2), **common,
        ),
    ]
