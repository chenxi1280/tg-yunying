from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (
    AuditLog, AuthorizationDrExecutionNode, TgAccount, TgAccountAuthorization,
    TgAuthorizationDrBatch, TgAuthorizationDrBatchItem,
    TgAuthorizationOnlineAbcBatch, TgPostLoginAbcRequest,
)
from app.services._common import _now, gateway
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.readiness import ABC_CAPABILITY_VERSION, MY_NODE_STALE_SECONDS
from app.services.authorization_dr.post_login_exception import (
    ACTION, apply_post_login_abc_exception, preview_post_login_abc_exception,
)
from tests import test_authorization_online_abc as base
from tests import test_authorization_online_abc_post_login as post_login
from tests import test_authorization_online_abc_sweep as sweep

pytestmark = pytest.mark.no_postgres
APPROVAL = dict(runtime_release_sha="b" * 40, idempotency_key="post-login-isolate-v1",
                requested_by="requester", approved_by="approver", approval_ref="POST-LOGIN-ABC")


@pytest.fixture
def state():
    fixture = base.session.__wrapped__()
    session = next(fixture)
    batch_id = post_login._post_login_batch(session)
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = post_login._item(session, batch_id)
    operation = base._add_operation(session, item.account_id, f"online-abc:{batch_id}:1:e4", "reconcile_unknown")
    batch.status = item.status = "stopped"
    item.outcome = "reconcile_unknown"
    operation.blocker_code = item.blocker_code = "目标无效"
    session.add(TgPostLoginAbcRequest(
        tenant_id=1, account_id=item.account_id, full_initialization_id=1,
        abc_batch_id=batch_id, status="manual_required", deployed_release_sha=base.RELEASE_SHA,
        requested_by="requester", approved_by="approver", approval_ref="POST-LOGIN-ABC",
    ))
    session.commit()
    session.autoflush = False
    yield session, batch, item, operation
    session.close()
    try:
        next(fixture)
    except StopIteration:
        pass


def test_isolation_keeps_unknown_and_a_without_remote_calls_or_release_rebind(state, monkeypatch):
    session, batch, item, operation = state
    before = sweep._a_snapshot(session, item.account_id)
    release = batch.execution_release_sha
    for name in ("send_message", "start_login", "authorization_identity"):
        monkeypatch.setattr(gateway, name, lambda *_a, **_k: pytest.fail("unexpected Telegram call"))
    preview = preview_post_login_abc_exception(session, batch.id, account_id=item.account_id, **APPROVAL)
    result = apply_post_login_abc_exception(session, batch.id, account_id=item.account_id,
                                          expected_fingerprint=preview["fingerprint"], **APPROVAL)
    assert result["batch_status"] == "completed_with_exceptions"
    assert result["request_status"] == "reconcile_unknown"
    assert (operation.status, operation.remote_call_state, operation.reconcile_status) == (
        "deferred_reconcile", "unknown", "quarantined")
    assert sweep._a_snapshot(session, item.account_id) == before
    assert batch.execution_release_sha == release
    repeated = apply_post_login_abc_exception(session, batch.id, account_id=item.account_id,
                                            expected_fingerprint=preview["fingerprint"], **APPROVAL)
    assert repeated["already_applied"] is True
    assert len(list(session.scalars(select(AuditLog).where(AuditLog.action == ACTION)))) == 1


@pytest.mark.parametrize("drift", ("owner", "lease", "generation", "batch_mode", "account", "approval", "active", "other_unknown"))
def test_unsafe_preview_is_rejected_without_mutation(state, drift):
    session, batch, item, operation = state
    approval = dict(APPROVAL)
    account_id = item.account_id
    if drift == "owner":
        operation.owner_node_id = "owner"
    elif drift == "lease":
        operation.lease_expires_at = _now() + timedelta(minutes=1)
    elif drift == "generation":
        session.get(TgAccount, item.account_id).connection_generation += 1
    elif drift == "batch_mode":
        batch.selection_mode = "all_online_accounts"
    elif drift == "account":
        account_id += 1
    elif drift == "approval":
        approval["approved_by"] = "another-actor"
    elif drift == "active":
        operation.status = "send_remote_started"
        operation.remote_call_state = "started"
    else:
        base._add_operation(session, base.ACCOUNT_IDS[1], "other-unknown", "reconcile_unknown")
    session.commit()
    with pytest.raises(AuthorizationDrError):
        preview_post_login_abc_exception(session, batch.id, account_id=account_id, **approval)
    assert operation.status == ("send_remote_started" if drift == "active" else "reconcile_unknown")
    assert item.status == "stopped"


def test_operation_version_drift_rejects_apply(state):
    session, batch, item, operation = state
    preview = preview_post_login_abc_exception(session, batch.id, account_id=item.account_id, **APPROVAL)
    operation.operation_version += 1
    session.commit()
    with pytest.raises(AuthorizationDrError, match="preview changed"):
        apply_post_login_abc_exception(session, batch.id, account_id=item.account_id,
                                      expected_fingerprint=preview["fingerprint"], **APPROVAL)
    assert operation.status == "reconcile_unknown"
    assert item.status == "stopped"


@pytest.fixture
def retired_c(state):
    session, batch, item, operation = state
    operation.operation_type = "provision_standby_2"
    operation.logical_slot = "standby_2"
    operation.status = "provision_reconcile_unknown"
    operation.owner_node_id = "my-retired"
    operation.owner_epoch = 1
    operation.updated_at = _now() - timedelta(seconds=30)
    migration = TgAuthorizationDrBatch(
        tenant_id=1, idempotency_key=f"online-abc:{batch.id}:1:b:c",
        target_set_fingerprint="c" * 64, target_count=1, status="reconcile_required",
        requested_by="requester", approved_by="approver", approval_ref="POST-LOGIN-ABC",
    )
    session.add(migration)
    session.flush()
    session.add(TgAuthorizationDrBatchItem(
        batch_id=migration.id, tenant_id=1, account_id=item.account_id, ordinal=1,
        expected_source_authorization_id=item.primary_authorization_id,
        expected_source_fact_version=1, expected_source_generation=1, target_generation=1,
        status="reconcile_unknown", outcome="reconcile_unknown", operation_id=operation.id,
    ))
    node = AuthorizationDrExecutionNode(
        id="my-retired", region_code="my", purpose="standby_session_dr",
        capability_version=ABC_CAPABILITY_VERSION, runtime_image_sha="c" * 40,
        standby_egress_id="my-direct", status="ready", active_client_count=0,
        last_heartbeat_at=_now(),
    )
    session.add(node)
    session.commit()
    base._add_operation(session, item.account_id, f"online-abc:{batch.id}:1:b", "succeeded")
    return session, batch, item, operation, node


def test_retired_c_owner_is_preserved_when_unknown_is_isolated(retired_c, monkeypatch):
    session, batch, item, operation, node = retired_c
    before = sweep._a_snapshot(session, item.account_id)
    sessions = [(a.id, a.session_ciphertext) for a in session.scalars(select(TgAccountAuthorization))]
    for name in ("send_message", "start_login", "authorization_identity"):
        monkeypatch.setattr(gateway, name, lambda *_a, **_k: pytest.fail("unexpected Telegram call"))
    preview = preview_post_login_abc_exception(session, batch.id, account_id=item.account_id, **APPROVAL)
    node.last_heartbeat_at = _now()
    node.version += 1
    session.commit()
    result = apply_post_login_abc_exception(session, batch.id, account_id=item.account_id,
                                          expected_fingerprint=preview["fingerprint"], **APPROVAL)
    assert result["request_status"] == "reconcile_unknown"
    assert (operation.status, operation.remote_call_state, operation.reconcile_status) == (
        "deferred_reconcile", "unknown", "quarantined")
    assert (operation.owner_node_id, operation.owner_epoch) == ("my-retired", 1)
    assert sweep._a_snapshot(session, item.account_id) == before
    assert [(a.id, a.session_ciphertext) for a in session.scalars(select(TgAccountAuthorization))] == sessions
    repeated = apply_post_login_abc_exception(session, batch.id, account_id=item.account_id,
                                            expected_fingerprint=preview["fingerprint"], **APPROVAL)
    assert repeated["already_applied"] is True
    logs = list(session.scalars(select(AuditLog).where(AuditLog.action == ACTION)))
    assert len(logs) == 1 and '"node_id": "my-retired"' in logs[0].detail


@pytest.mark.parametrize("drift", ("busy", "stale", "before_unknown", "missing_heartbeat", "lease", "lease_token", "region", "purpose", "type"))
def test_retired_c_requires_current_idle_owner_evidence(retired_c, drift):
    session, batch, item, operation, node = retired_c
    if drift == "busy":
        node.active_client_count = 1
    elif drift == "stale":
        node.last_heartbeat_at = _now() - timedelta(seconds=MY_NODE_STALE_SECONDS + 1)
    elif drift == "before_unknown":
        node.last_heartbeat_at = operation.updated_at - timedelta(seconds=1)
    elif drift == "missing_heartbeat":
        node.last_heartbeat_at = None
    elif drift == "lease":
        operation.lease_expires_at = _now() - timedelta(seconds=1)
    elif drift == "lease_token":
        operation.lease_token = "unfinished-lease"
    elif drift == "region":
        node.region_code = "sv"
    elif drift == "purpose":
        node.purpose = "another-purpose"
    else:
        operation.operation_type = "migrate_standby_2"
    session.commit()
    with pytest.raises(AuthorizationDrError):
        preview_post_login_abc_exception(session, batch.id, account_id=item.account_id, **APPROVAL)
    assert (operation.status, item.status) == ("provision_reconcile_unknown", "stopped")


def test_retired_c_node_image_drift_rejects_apply(retired_c):
    session, batch, item, operation, node = retired_c
    preview = preview_post_login_abc_exception(session, batch.id, account_id=item.account_id, **APPROVAL)
    node.runtime_image_sha = "d" * 40
    session.commit()
    with pytest.raises(AuthorizationDrError, match="preview changed"):
        apply_post_login_abc_exception(session, batch.id, account_id=item.account_id,
                                      expected_fingerprint=preview["fingerprint"], **APPROVAL)
    assert (operation.status, item.status) == ("provision_reconcile_unknown", "stopped")


@pytest.mark.parametrize("drift", ("busy", "old_heartbeat"))
def test_retired_c_idle_evidence_is_revalidated_at_apply(retired_c, drift):
    session, batch, item, operation, node = retired_c
    preview = preview_post_login_abc_exception(session, batch.id, account_id=item.account_id, **APPROVAL)
    if drift == "busy":
        node.active_client_count = 1
    else:
        node.last_heartbeat_at = operation.updated_at - timedelta(seconds=1)
    session.commit()
    with pytest.raises(AuthorizationDrError):
        apply_post_login_abc_exception(session, batch.id, account_id=item.account_id,
                                      expected_fingerprint=preview["fingerprint"], **APPROVAL)
    assert (operation.status, item.status) == ("provision_reconcile_unknown", "stopped")
