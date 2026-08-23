from __future__ import annotations

import pytest
from sqlalchemy import select

import app.services.authorization_dr.online_abc_completed_recovery as completed
import app.services.authorization_dr.online_abc_runner as runner
from app.models import (
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
    TgAuthorizationWakeBundle,
)
from app.services._common import _now
from app.services.authorization_dr.online_abc import start_next_online_abc_item, sync_online_abc_batch
from tests import test_authorization_online_abc as abc_tests
from tests import test_authorization_online_abc_c_orphan as orphan_tests


pytestmark = pytest.mark.no_postgres
RESUMED_RELEASE_SHA = "b" * 40


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


def test_completed_rebase_retains_c_and_creates_new_b_and_e4(
    db_session, monkeypatch,
) -> None:
    batch, item, old_primary, c_operation, old_e4 = _completed_drift(db_session)
    account, _old, new_primary, case = orphan_tests._seed_verified_local_activate(
        db_session, item, c_operation,
    )
    _restore_completed_checkpoint(db_session, item, c_operation)
    preview = completed.preview_completed_rebase(
        db_session, batch.id, item.account_id, case.id,
        idempotency_key="completed-rebase-1",
    )
    result = completed.apply_completed_rebase(
        db_session, batch.id, item.account_id, case.id,
        idempotency_key="completed-rebase-1",
        expected_fingerprint=preview["fingerprint"],
        requested_by="requester", approved_by="approver", approval_ref="ABC-10",
    )

    assert result["outcome"] == "runner_blocked"
    assert result["blocker_code"] == completed.COMPLETED_REBASE_BLOCKER
    assert result["primary_authorization_id"] == new_primary.id
    assert old_primary.health_status == "invalid"
    assert c_operation.status == "succeeded"
    assert old_e4.status == "succeeded"
    resumed = runner.resume_online_abc_batch(
        db_session, batch.id, account_id=item.account_id,
        requested_by="requester", approved_by="approver", approval_ref="ABC-10",
        runtime_release_sha=RESUMED_RELEASE_SHA,
    )
    assert resumed["batch"]["status"] == "running"

    calls: list[tuple[str, int]] = []
    abc_tests._mock_runner_success(monkeypatch, calls)
    finished = runner.run_online_abc_batch(
        db_session, batch.id,
        requested_by="requester", approved_by="approver", approval_ref="ABC-10",
        runtime_release_sha=RESUMED_RELEASE_SHA, max_accounts=1,
    )

    db_session.refresh(account)
    assert finished["chunk"]["account_ids"] == [item.account_id]
    assert calls == [("b", item.account_id), ("qualify", item.account_id), ("e4", item.account_id)]
    operations = runner.online_abc_item_operations(db_session, batch, item)
    assert operations["c"].id == c_operation.id
    assert operations["e4"].id != old_e4.id
    assert operations["e4"].idempotency_key.endswith(":retry:1")
    assert account.current_authorization_id == new_primary.id


def test_pre_remote_rearm_restores_stopped_checkpoint(db_session) -> None:
    batch, item, operation = orphan_tests._stopped_unknown_item(db_session)
    _account, _old, _new, case = orphan_tests._seed_verified_local_activate(
        db_session, item, operation,
    )
    orphan_tests._apply_rebase_twice(db_session, batch, item, case)
    runner.resume_online_abc_batch(
        db_session, batch.id, account_id=item.account_id,
        requested_by="requester", approved_by="approver", approval_ref="ABC-10",
        runtime_release_sha=RESUMED_RELEASE_SHA,
    )
    _stop_for_other_completed_drift(db_session, batch.id, item.id)

    preview = completed.preview_pre_remote_rearm(
        db_session, batch.id, item.account_id, case.id,
        idempotency_key="pre-remote-rearm-1",
    )
    result = completed.apply_pre_remote_rearm(
        db_session, batch.id, item.account_id, case.id,
        idempotency_key="pre-remote-rearm-1",
        expected_fingerprint=preview["fingerprint"],
        requested_by="requester", approved_by="approver", approval_ref="ABC-10",
    )

    assert result["status"] == "stopped"
    assert result["outcome"] == "runner_blocked"
    assert result["blocker_code"] == "post_local_activate_rebase_ready"


def _completed_drift(session):
    batch_id = abc_tests._apply(session, abc_tests._preview(session)["fingerprint"])["batch_id"]
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-10")
    item = session.get(TgAuthorizationOnlineAbcItem, command["item_id"])
    old_primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    abc_tests._qualify_primary(session, item.account_id)
    abc_tests._add_operations(session, command, status="succeeded")
    _convert_b_to_prequalified(session, item)
    c_operation, e4_operation = _bind_completed_operations(session, command, item, old_primary)
    sync_online_abc_batch(session, batch_id, actor="approver", approval_ref="ABC-10")
    old_primary.health_status = "invalid"
    old_primary.last_authoritative_error_code = "authorization_key_duplicated"
    session.get(TgAccount, item.account_id).status = "Session失效"
    session.commit()
    sync_online_abc_batch(session, batch_id, actor="approver", approval_ref="ABC-10")
    return session.get(TgAuthorizationOnlineAbcBatch, batch_id), item, old_primary, c_operation, e4_operation


def _convert_b_to_prequalified(session, item) -> None:
    batch = session.get(TgAuthorizationOnlineAbcBatch, item.batch_id)
    operation = runner.online_abc_item_operations(session, batch, item)["b"]
    session.delete(operation)
    item.standby_1_plan = "already_qualified"
    slot = session.scalar(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item.id,
        TgAuthorizationOnlineAbcSlotResult.logical_slot == "standby_1",
    ))
    slot.outcome = "already_qualified"
    session.commit()


def _restore_completed_checkpoint(session, item, c_operation) -> None:
    item.outcome = completed.PRIMARY_DRIFT_OUTCOME
    item.blocker_code = completed.PRIMARY_DRIFT_OUTCOME
    c_operation.status = "succeeded"
    c_operation.remote_call_state = "succeeded"
    c_operation.reconcile_status = "none"
    c_operation.blocker_code = ""
    session.commit()


def _bind_completed_operations(session, command, item, old_primary):
    batch = session.get(TgAuthorizationOnlineAbcBatch, item.batch_id)
    operations = runner.online_abc_item_operations(session, batch, item)
    candidate = TgAccountAuthorization(
        tenant_id=1, account_id=item.account_id, role="standby_2", logical_slot="standby_2",
        slot_generation=2, is_slot_current=True, provision_region_code="my",
        developer_app_id=3, proxy_id=old_primary.proxy_id, session_ciphertext="qualified-c",
        status="standby", health_status="healthy", is_current=False,
        protected_from_cleanup=True, telegram_user_id_digest="1" * 64,
        auth_key_fingerprint_digest="8" * 64,
    )
    source = session.get(TgAccountAuthorization, item.source_c_authorization_id)
    source.is_slot_current = False
    session.add(candidate)
    session.flush()
    bundle = TgAuthorizationWakeBundle(
        tenant_id=1, account_id=item.account_id, authorization_id=candidate.id,
        operation_id=operations["c"].id, bundle_generation=1,
        ciphertext_digest="c" * 64, wrapped_dek_ciphertext="wrapped",
        kms_key_ref_digest="k" * 64, kms_key_version="v1",
        auth_key_fingerprint_digest=candidate.auth_key_fingerprint_digest,
        telegram_user_id_digest=candidate.telegram_user_id_digest,
    )
    session.add(bundle)
    session.flush()
    candidate.wake_bundle_id = bundle.id
    operations["c"].candidate_authorization_id = candidate.id
    operations["c"].expected_current_authorization_id = old_primary.id
    operations["e4"].expected_current_authorization_id = old_primary.id
    session.commit()
    return operations["c"], operations["e4"]


def _stop_for_other_completed_drift(session, batch_id: str, item_id: str) -> None:
    other = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.id != item_id,
    ).limit(1))
    other.status = "stopped"
    other.outcome = completed.PRIMARY_DRIFT_OUTCOME
    other.blocker_code = completed.PRIMARY_DRIFT_OUTCOME
    other.finished_at = _now()
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    batch.status = "stopped"
    session.commit()
