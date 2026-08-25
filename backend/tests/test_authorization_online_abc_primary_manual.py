from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import (
    AccountStatus,
    TgAccount,
    TgAccountAuthorization,
    TgAccountOnlineState,
    TgAuthorizationRestoreProbeFact,
    TgAuthorizationWakeBundle,
    TgAuthorizationWakeBundleCopy,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
)
from app.services._common import _now
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc import start_next_online_abc_item, sync_online_abc_batch
from app.services.authorization_dr.online_abc_manifest import (
    apply_full_online_abc_batch,
    preview_full_online_abc_batch,
)
from app.services.authorization_dr.online_abc_chunk import pause_online_abc_chunk
from app.services.authorization_dr.online_abc_primary_manual import (
    B_FAILURE_CODE,
    MANUAL_BLOCKER,
    apply_primary_failure_manual_outcome,
    preview_primary_failure_manual_outcome,
)
import app.services.authorization_dr.online_abc_primary_manual as primary_manual
import app.services.authorization_dr.online_abc_runner as runner
from tests import test_authorization_online_abc as abc_tests


pytestmark = pytest.mark.no_postgres
NEW_RELEASE_SHA = "d" * 40
MANUAL_KEY = "abc-primary-manual:test:101:v1"
MANUAL_REF = "user-approved-primary-double-failure-skip"


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


def test_preview_freezes_typed_a_b_failure_and_complete_c(db_session, monkeypatch) -> None:
    batch_id, account_id, _, _ = _completed_primary_failure(db_session)
    _reject_b(monkeypatch)
    before = _snapshot(db_session, batch_id, account_id)

    preview = _preview(db_session, batch_id, account_id)

    assert preview["standby_b_failure"] == B_FAILURE_CODE
    assert preview["primary_failure"][1] == "login_required"
    assert preview["bundle"][3] == 2
    assert preview["bundle"][5] == "passed"
    assert preview["global"]["unknown"] == 0
    assert _snapshot(db_session, batch_id, account_id) == before
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_apply_preserves_a_and_c_marks_b_invalid_and_continues(db_session, monkeypatch) -> None:
    batch_id, account_id, b_id, c_id = _completed_primary_failure(db_session)
    _reject_b(monkeypatch)
    before_a = _a_snapshot(db_session, account_id)
    before_c = _authorization_snapshot(db_session, c_id)
    preview = _preview(db_session, batch_id, account_id)

    result = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])

    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = _item(db_session, batch_id, account_id)
    standby_b = db_session.get(TgAccountAuthorization, b_id)
    assert result["item_outcome"] == "manual_required"
    assert result["b_outcome"] == "manual_required"
    assert result["c_outcome"] == "succeeded"
    assert batch.status == "running" and batch.execution_release_sha == NEW_RELEASE_SHA
    assert item.blocker_code == MANUAL_BLOCKER
    assert (standby_b.status, standby_b.health_status) == ("invalid", "invalid")
    assert standby_b.last_authoritative_error_code == B_FAILURE_CODE
    assert _a_snapshot(db_session, account_id) == before_a
    assert _authorization_snapshot(db_session, c_id) == before_c


def test_acknowledged_primary_debt_does_not_block_later_tail(db_session, monkeypatch) -> None:
    batch_id, account_id, _, _ = _completed_primary_failure(db_session)
    _reject_b(monkeypatch)
    preview = _preview(db_session, batch_id, account_id)
    _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    abc_tests._mock_runner_success(monkeypatch, [])

    result = runner.run_online_abc_batch(
        db_session, batch_id,
        requested_by="requester", approved_by="approver", approval_ref="ABC-FULL",
        runtime_release_sha=NEW_RELEASE_SHA, max_accounts=30, sleeper=lambda _: None,
    )

    assert result["batch"]["status"] == "completed_with_manual"
    assert result["batch"]["account_outcome_counts"] == {
        "manual_required": 1,
        "succeeded": 9,
    }
    assert _item(db_session, batch_id, account_id).blocker_code == MANUAL_BLOCKER


def test_acknowledged_primary_debt_still_stops_on_new_a_drift(db_session, monkeypatch) -> None:
    batch_id, account_id, _, _ = _completed_primary_failure(db_session)
    _reject_b(monkeypatch)
    preview = _preview(db_session, batch_id, account_id)
    _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    db_session.get(TgAccount, account_id).connection_generation += 1
    db_session.commit()
    abc_tests._mock_runner_success(monkeypatch, [])

    with pytest.raises(AuthorizationDrError) as exc_info:
        runner.run_online_abc_batch(
            db_session, batch_id,
            requested_by="requester", approved_by="approver", approval_ref="ABC-FULL",
            runtime_release_sha=NEW_RELEASE_SHA, max_accounts=30, sleeper=lambda _: None,
        )

    assert exc_info.value.code == "online_abc_primary_drift"


def test_acknowledged_primary_debt_allows_chunk_pause(db_session, monkeypatch) -> None:
    batch_id, account_id, _, _, running_id = _completed_primary_failure(
        db_session, with_inflight=True,
    )
    _reject_b(monkeypatch)
    preview = _preview(db_session, batch_id, account_id)
    _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    sync_online_abc_batch(db_session, batch_id, actor="approver", approval_ref="ABC-FULL")

    paused = pause_online_abc_chunk(
        db_session, batch_id, actor="approver", approval_ref="ABC-FULL", processed_count=1,
    )

    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    assert paused is True and batch.status == "stopped"
    assert _item(db_session, batch_id, account_id).blocker_code == MANUAL_BLOCKER
    assert _item(db_session, batch_id, running_id).status == "succeeded"


def test_chunk_pause_rejects_new_acknowledged_a_drift(db_session, monkeypatch) -> None:
    batch_id, account_id, _, _ = _completed_primary_failure(db_session)
    _reject_b(monkeypatch)
    preview = _preview(db_session, batch_id, account_id)
    _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    db_session.get(TgAccount, account_id).connection_generation += 1
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        pause_online_abc_chunk(
            db_session, batch_id, actor="approver", approval_ref="ABC-FULL", processed_count=1,
        )

    assert exc_info.value.code == "online_abc_primary_drift"
    assert db_session.get(TgAuthorizationOnlineAbcBatch, batch_id).status == "running"


def test_preview_rejects_usable_b_and_missing_c_evidence(db_session, monkeypatch) -> None:
    batch_id, account_id, _, c_id = _completed_primary_failure(db_session)
    monkeypatch.setattr(primary_manual.gateway, "authorization_identity", lambda *_args: object())
    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)
    assert exc_info.value.code == "online_abc_standby_still_usable"

    _reject_b(monkeypatch)
    db_session.get(TgAccountAuthorization, c_id).wake_bundle_id = None
    db_session.commit()
    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)
    assert exc_info.value.code == "migration_artifact_incomplete"


def test_apply_is_idempotent_and_rejects_changed_fingerprint(db_session, monkeypatch) -> None:
    batch_id, account_id, _, _ = _completed_primary_failure(db_session)
    _reject_b(monkeypatch)
    preview = _preview(db_session, batch_id, account_id)

    first = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    second = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])

    assert first["already_applied"] is False
    assert second["already_applied"] is True
    with pytest.raises(AuthorizationDrError) as exc_info:
        _apply(db_session, batch_id, account_id, fingerprint="f" * 64)
    assert exc_info.value.code == "idempotency_key_conflict"


def test_apply_preserves_completed_running_checkpoint(db_session, monkeypatch) -> None:
    batch_id, account_id, _, _, running_id = _completed_primary_failure(
        db_session, with_inflight=True,
    )
    _reject_b(monkeypatch)
    before = _snapshot(db_session, batch_id, running_id)[2:]
    preview = _preview(db_session, batch_id, account_id)

    result = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])

    assert preview["inflight_checkpoint"][1] == running_id
    assert result["batch_status"] == "running"
    assert _snapshot(db_session, batch_id, running_id)[2:] == before


def _completed_primary_failure(session, *, with_inflight: bool = False):
    batch_id = _full_batch(session)
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-FULL")
    abc_tests._qualify_primary(session, command["account_id"])
    abc_tests._add_operations(session, command, status="succeeded")
    item = _item(session, batch_id, command["account_id"])
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    b_id, c_id = _bind_authorizations(session, batch_id, item, primary=primary)
    sync_online_abc_batch(session, batch_id, actor="approver", approval_ref="ABC-FULL")
    running_id = _completed_running_item(session, batch_id) if with_inflight else None
    account = session.get(TgAccount, item.account_id)
    account.status = AccountStatus.NEED_RELOGIN.value
    session.add(TgAccountOnlineState(
        tenant_id=1, account_id=account.id, desired_online=True,
        online_status="login_required", failure_detail="session 已失效",
        last_probe_at=_now(), last_seen_at=_now(),
    ))
    session.commit()
    sync_online_abc_batch(session, batch_id, actor="approver", approval_ref="ABC-FULL")
    values = (batch_id, item.account_id, b_id, c_id)
    return (*values, running_id) if with_inflight else values


def _completed_running_item(session, batch_id: str) -> int:
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-FULL")
    abc_tests._qualify_primary(session, command["account_id"])
    abc_tests._add_operations(session, command, status="succeeded")
    item = _item(session, batch_id, command["account_id"])
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    _bind_authorizations(session, batch_id, item, primary=primary)
    return item.account_id


def _bind_authorizations(session, batch_id, item, *, primary) -> tuple[int, int]:
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    operations = runner.online_abc_item_operations(session, batch, item)
    standby_b = _authorization(item, primary, logical_slot="standby_1", app_id=1, auth_digest="3" * 64)
    standby_c = _authorization(item, primary, logical_slot="standby_2", app_id=3, auth_digest="8" * 64)
    standby_c.provision_region_code = "my"
    standby_c.session_ciphertext = None
    session.get(TgAccountAuthorization, item.source_c_authorization_id).is_slot_current = False
    session.add_all([standby_b, standby_c])
    session.flush()
    operations["b"].candidate_authorization_id = standby_b.id
    operations["c"].candidate_authorization_id = standby_c.id
    _bind_bundle(session, operations["c"].id, standby_c)
    session.commit()
    return standby_b.id, standby_c.id


def _authorization(item, primary, *, logical_slot: str, app_id: int, auth_digest: str):
    return TgAccountAuthorization(
        tenant_id=1, account_id=item.account_id, role=logical_slot, logical_slot=logical_slot,
        slot_generation=2, is_slot_current=True, provision_region_code="sv",
        developer_app_id=app_id, proxy_id=primary.proxy_id,
        session_ciphertext=f"{logical_slot}-{item.account_id}",
        status="standby", health_status="healthy", is_current=False,
        protected_from_cleanup=True, telegram_user_id_digest=primary.telegram_user_id_digest,
        auth_key_fingerprint_digest=auth_digest,
    )


def _bind_bundle(session, operation_id: str, standby_c) -> None:
    bundle = TgAuthorizationWakeBundle(
        tenant_id=1, account_id=standby_c.account_id, authorization_id=standby_c.id,
        operation_id=operation_id, bundle_generation=1, ciphertext_digest="c" * 64,
        wrapped_dek_ciphertext="wrapped", kms_key_ref_digest="k" * 64,
        kms_key_version="v1", kms_decrypt_status="verified",
        auth_key_fingerprint_digest=standby_c.auth_key_fingerprint_digest,
        telegram_user_id_digest=standby_c.telegram_user_id_digest,
        recoverable_copy_count=2, receipt_status="active", is_active=True,
        protected_from_cleanup=True,
    )
    session.add(bundle)
    session.flush()
    standby_c.wake_bundle_id = bundle.id
    for kind in ("local_persistent", "remote_ssh_snapshot"):
        session.add(_copy(bundle.id, kind))
    session.add(TgAuthorizationRestoreProbeFact(
        bundle_id=bundle.id, operation_id=operation_id, probe_generation=1,
        source_copy_kind="remote_ssh_snapshot", status="passed",
        session_parse_status="passed", authorization_status="authorized",
        identity_match_status="matched", auth_key_match_status="matched",
        source_client_disconnected=True, probe_client_disconnected=True,
        zeroize_receipt_digest="z" * 64,
    ))


def _copy(bundle_id: str, kind: str):
    return TgAuthorizationWakeBundleCopy(
        bundle_id=bundle_id, copy_kind=kind, object_ref_digest="o" * 64,
        ciphertext_digest="c" * 64, immutable_version=f"immutable-{kind}",
        write_receipt_digest="w" * 64, readback_receipt_digest="r" * 64,
        write_verified_at=_now(), readback_verified_at=_now(), decrypt_verified_at=_now(),
    )


def _full_batch(session) -> str:
    abc_tests._seed_accepted_canary(session)
    preview = preview_full_online_abc_batch(
        session, 1, idempotency_key="primary-manual-full", deployed_release_sha=abc_tests.RELEASE_SHA,
    )
    return apply_full_online_abc_batch(
        session, 1, idempotency_key="primary-manual-full",
        deployed_release_sha=abc_tests.RELEASE_SHA, expected_fingerprint=preview["fingerprint"],
        requested_by="requester", approved_by="approver", approval_ref="ABC-FULL",
    )["batch_id"]


def _preview(session, batch_id: str, account_id: int) -> dict:
    return preview_primary_failure_manual_outcome(
        session, batch_id, account_id, runtime_release_sha=NEW_RELEASE_SHA,
        idempotency_key=MANUAL_KEY, requested_by="requester", approved_by="approver",
        approval_ref=MANUAL_REF,
    )


def _apply(session, batch_id: str, account_id: int, *, fingerprint: str) -> dict:
    return apply_primary_failure_manual_outcome(
        session, batch_id, account_id, runtime_release_sha=NEW_RELEASE_SHA,
        idempotency_key=MANUAL_KEY, expected_fingerprint=fingerprint,
        requested_by="requester", approved_by="approver", approval_ref=MANUAL_REF,
    )


def _reject_b(monkeypatch) -> None:
    def reject(*_args):
        raise RuntimeError("session is not authorized")

    monkeypatch.setattr(primary_manual.gateway, "authorization_identity", reject)


def _item(session, batch_id: str, account_id: int):
    return session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))


def _a_snapshot(session, account_id: int) -> tuple:
    account = session.get(TgAccount, account_id)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    return (
        account.current_authorization_id, account.status, account.session_ciphertext,
        account.authorization_generation, account.authorization_fact_generation,
        account.connection_generation, primary.session_ciphertext, primary.fact_version,
        primary.status, primary.health_status,
    )


def _authorization_snapshot(session, authorization_id: int) -> tuple:
    row = session.get(TgAccountAuthorization, authorization_id)
    return row.status, row.health_status, row.fact_version, row.wake_bundle_id


def _snapshot(session, batch_id: str, account_id: int) -> tuple:
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = _item(session, batch_id, account_id)
    return batch.status, batch.version, item.status, item.outcome, item.version, _a_snapshot(session, account_id)
