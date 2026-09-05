from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.models import (
    TgAuthorizationDrOperation, TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem, TgAuthorizationOnlineAbcSlotResult, TgPostLoginAbcRequest,
)
from app.services._common import _now, audit, gateway
from app.services.authorization_dr.artifact_reconcile import claim_artifact_reconcile
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc_post_bundle_interrupt import (
    apply_post_bundle_interrupt, preview_post_bundle_interrupt,
)
from app.services.authorization_dr.online_abc_runner import resume_online_abc_batch
from app.services.authorization_dr.slot import commit_migration_slot
from app.services.authorization_dr.wake_bundle import commit_wake_bundle_receipt, record_restore_probe
from tests import test_authorization_online_abc_post_bundle_interrupt as base

pytestmark = pytest.mark.no_postgres
db_session = base.db_session
APPROVAL = dict(runtime_release_sha=base.NEW_RELEASE_SHA, idempotency_key=base.KEY,
                requested_by="requester", approved_by="approver", approval_ref="ABC-FULL",
                interruption_ref="my-log:post-login-restore-probe-422")


@pytest.fixture
def state(db_session):
    batch_id, account_id, operation_id, receipt = base._post_bundle_interrupt(db_session)
    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    others = select(TgAuthorizationOnlineAbcItem.id).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id != account_id,
    )
    db_session.execute(delete(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id.in_(others)))
    db_session.execute(delete(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.id.in_(others)))
    batch.selection_mode = "post_login_exact"
    batch.target_count = 1
    request = TgPostLoginAbcRequest(
        tenant_id=batch.tenant_id, account_id=account_id, full_initialization_id=1,
        abc_batch_id=batch_id, status="running", deployed_release_sha=batch.deployed_release_sha,
        requested_by=batch.requested_by, approved_by=batch.approved_by, approval_ref=batch.approval_ref,
    )
    db_session.add(request)
    db_session.commit()
    db_session.autoflush = False
    return db_session, batch, request, operation_id, receipt


def test_exact_post_login_recovers_original_c_to_post_c_checkpoint(state, monkeypatch):
    session, batch, request, operation_id, receipt = state
    before = base._artifact_snapshot(session, operation_id)
    before_a = base.interrupt_tests._a_snapshot(session, request.account_id)
    monkeypatch.setattr(gateway, "start_login", lambda *_a, **_k: pytest.fail("unexpected new login"))
    preview = preview_post_bundle_interrupt(session, batch.id, request.account_id, **APPROVAL)
    assert preview["post_login_request"]["version"] == request.request_version
    result = apply_post_bundle_interrupt(session, batch.id, request.account_id,
                                         expected_fingerprint=preview["fingerprint"], **APPROVAL)
    assert result["batch_status"] == "stopped"
    assert base._artifact_snapshot(session, operation_id) == before
    claim = claim_artifact_reconcile(session, operation_id, "my-node-1")
    owner = dict(node_id="my-node-1", owner_epoch=claim["owner_epoch"], lease_token=claim["lease_token"])
    commit_wake_bundle_receipt(session, operation_id, receipt, **owner)
    record_restore_probe(session, operation_id, base._probe_receipt(receipt.bundle_generation), **owner)
    commit_migration_slot(session, operation_id, **owner)
    result = resume_online_abc_batch(session, batch.id, account_id=request.account_id,
                                    **{key: APPROVAL[key] for key in
                                       ("requested_by", "approved_by", "approval_ref", "runtime_release_sha")})
    assert result["next_action"] == "verify_e4"
    assert base.interrupt_tests._a_snapshot(session, request.account_id) == before_a


@pytest.mark.parametrize("drift", ("request", "version", "lease", "scope", "approval_ref"))
def test_post_login_guard_drift_rejects_apply(state, drift):
    session, batch, request, operation_id, _receipt = state
    preview = preview_post_bundle_interrupt(session, batch.id, request.account_id, **APPROVAL)
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    approval = dict(APPROVAL)
    if drift == "request":
        request.approved_by = "other-approver"
    elif drift == "version":
        request.request_version += 1
    elif drift == "lease":
        operation.lease_expires_at = _now() + timedelta(minutes=1)
    elif drift == "scope":
        batch.target_count = 2
    else:
        approval["approval_ref"] = "unrelated-approval"
    session.commit()
    with pytest.raises(AuthorizationDrError):
        apply_post_bundle_interrupt(session, batch.id, request.account_id,
                                    expected_fingerprint=preview["fingerprint"], **approval)
    assert operation.reconcile_status == "none"
    assert batch.status == "running"


@pytest.mark.parametrize("stop_audit", (True, False))
def test_release_stopped_post_login_requires_original_stop_audit(state, stop_audit):
    session, batch, request, _operation_id, _receipt = state
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id))
    batch.status = item.status = "stopped"
    item.outcome = "runner_blocked"
    item.blocker_code = "runtime_image_mismatch"
    request.status = "manual_required"
    if stop_audit:
        audit(session, tenant_id=batch.tenant_id, actor=batch.approved_by,
              action="停止 post-login exact ABC 自动执行",
              target_type="tg_authorization_online_abc_batches", target_id=batch.id,
              detail="approval_ref=ABC-FULL; blocker=runtime_image_mismatch; no_replay=true")
    session.commit()
    if not stop_audit:
        with pytest.raises(AuthorizationDrError):
            preview_post_bundle_interrupt(session, batch.id, request.account_id, **APPROVAL)
        return
    preview = preview_post_bundle_interrupt(session, batch.id, request.account_id, **APPROVAL)
    apply_post_bundle_interrupt(session, batch.id, request.account_id,
                                expected_fingerprint=preview["fingerprint"], **APPROVAL)
    assert batch.execution_release_sha == APPROVAL["runtime_release_sha"]


def test_running_post_login_does_not_require_a_release_change(state):
    session, batch, request, _operation_id, _receipt = state
    approval = {**APPROVAL, "runtime_release_sha": batch.execution_release_sha}
    preview = preview_post_bundle_interrupt(session, batch.id, request.account_id, **approval)
    assert preview["previous_execution_release_sha"] == preview["runtime_release_sha"]
