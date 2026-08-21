from __future__ import annotations

import hashlib
from datetime import timedelta
from types import SimpleNamespace

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import AuthorizationIdentity
from app.models import (
    AccountProxy,
    AccountStatus,
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgAccountOnlineState,
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
    TgLoginFlow,
)
from app.security import encrypt_session
from app.services._common import _now
from app.services.account_online_probe import OnlineProbeResult, _apply_probe_result
from app.services.authorization_dr import (
    apply_artifact_abandon,
    apply_local_activate,
    apply_operation_reconcile,
    build_pre_code_failure_evidence,
    claim_artifact_reconcile,
    mark_login_remote_unknown,
    preview_artifact_abandon,
    preview_local_activate,
    preview_operation_reconcile,
    project_authoritative_login_failure,
)
from app.workers.authorization_dr_artifact_recovery import _recover_receipt
from app.workers.authorization_dr_kms import WrappedDek
from app.workers.authorization_dr_node import _encrypt_session
from scripts.authorization_dr_phone_ban_projection import (
    apply_phone_ban_projection,
    preview_phone_ban_projection,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _identity() -> AuthorizationIdentity:
    return AuthorizationIdentity("123", "a" * 64, "b" * 64, "c" * 64)


def test_local_activate_probes_and_advances_all_current_generations(monkeypatch) -> None:
    with _session() as session:
        account, target = _seed_local_activate(session)
        old_session = account.session_ciphertext
        invalidated = []
        monkeypatch.setattr(
            "app.services.authorization_dr.local_activate.credentials_for_developer_app",
            lambda *_args: SimpleNamespace(),
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.local_activate.credentials_for_authorization",
            lambda *_args, **_kwargs: SimpleNamespace(),
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.local_activate.gateway.authorization_identity",
            lambda *_args: _identity(),
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.local_activate.gateway.invalidate_session_cache",
            lambda session_ciphertext, _credentials: invalidated.append(session_ciphertext) or 1,
        )
        case = preview_local_activate(session, 1, account.id, target.id, actor="requester", reason="primary invalid")
        applied = apply_local_activate(
            session,
            1,
            account.id,
            target.id,
            fingerprint=case.fingerprint,
            actor="approver",
            approval_ref="INC-LOCAL-ACTIVATE",
            idempotency_key="activate-67",
        )
        session.refresh(account)
        state = session.query(TgAccountOnlineState).filter_by(account_id=account.id).one()

        assert applied.status == "applied"
        assert account.current_authorization_id == target.id
        assert account.authorization_generation == 5
        assert account.authorization_fact_generation == 8
        assert account.connection_generation == 10
        assert account.business_runtime_status == "warming"
        assert account.sv_redundancy_status == "degraded"
        assert account.session_ciphertext == target.session_ciphertext
        assert state.online_status == "warming"
        assert state.session_id == str(account.id)
        assert invalidated == [old_session]


def test_local_activate_bootstraps_missing_legacy_identity_after_two_probes(monkeypatch) -> None:
    with _session() as session:
        account, target = _seed_local_activate(session)
        target.telegram_user_id_digest = ""
        target.auth_key_fingerprint_digest = ""
        session.commit()
        probes = []
        monkeypatch.setattr(
            "app.services.authorization_dr.local_activate.credentials_for_developer_app",
            lambda *_args: SimpleNamespace(),
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.local_activate.credentials_for_authorization",
            lambda *_args, **_kwargs: SimpleNamespace(),
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.local_activate.gateway.authorization_identity",
            lambda *_args: probes.append(1) or _identity(),
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.local_activate.gateway.invalidate_session_cache",
            lambda *_args: 1,
        )

        case = preview_local_activate(session, 1, account.id, target.id, actor="requester", reason="legacy")
        apply_local_activate(
            session,
            1,
            account.id,
            target.id,
            fingerprint=case.fingerprint,
            actor="approver",
            approval_ref="INC-LOCAL-ACTIVATE",
            idempotency_key="activate-legacy-67",
        )

        session.refresh(target)
        assert probes == [1, 1]
        assert target.telegram_user_id_digest == "b" * 64
        assert target.auth_key_fingerprint_digest == "a" * 64
        assert target.telegram_authorization_hash_ciphertext


def test_phone_ban_projects_account_and_online_truth_without_deleting_session() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="tenant"))
        account = TgAccount(
            id=22,
            tenant_id=1,
            display_name="banned",
            phone_masked="22",
            session_ciphertext="preserved",
            status=AccountStatus.SESSION_EXPIRED.value,
        )
        session.add(account)
        session.add(TgAccountOnlineState(tenant_id=1, account_id=22, online_status="online"))
        session.commit()

        project_authoritative_login_failure(session, 22, "phone_number_banned")
        session.commit()
        state = session.query(TgAccountOnlineState).filter_by(account_id=22).one()

        assert account.status == AccountStatus.BANNED.value
        assert account.session_ciphertext == "preserved"
        assert state.online_status == "login_required"
        assert state.failure_type == "phone_number_banned"


def test_online_probe_cannot_overwrite_persisted_phone_ban_fact() -> None:
    with _session() as session:
        operation = _seed_unknown_operation(session)
        operation.status = "failed"
        operation.remote_call_state = "confirmed_no_effect"
        operation.blocker_code = "phone_number_banned"
        account = session.get(TgAccount, 26)
        account.status = AccountStatus.SESSION_EXPIRED.value
        state = TgAccountOnlineState(
            tenant_id=1,
            account_id=26,
            desired_online=True,
            online_status="login_required",
        )
        session.add(state)
        session.commit()

        health = SimpleNamespace(status=AccountStatus.ACTIVE.value, health_score=100, detail="authorized")
        _apply_probe_result(
            session,
            account,
            state,
            _now(),
            OnlineProbeResult(account_id=26, health=health),
        )
        session.commit()

        assert account.status == AccountStatus.BANNED.value
        assert account.health_score == 0
        assert state.desired_online is False
        assert state.failure_type == "phone_number_banned"


def test_artifact_reconcile_requires_approval_then_claims_only_while_runtime_off() -> None:
    with _session() as session:
        operation = _seed_unknown_operation(session)
        evidence = _artifact_evidence(operation)
        case = preview_operation_reconcile(
            session,
            operation.id,
            tenant_id=1,
            expected_operation_version=operation.operation_version,
            evidence=evidence,
            actor="requester",
        )
        apply_operation_reconcile(
            session,
            operation.id,
            tenant_id=1,
            expected_operation_version=operation.operation_version,
            evidence_fingerprint=case.evidence_fingerprint,
            approval_ref="INC-ARTIFACT",
            idempotency_key="artifact-26",
            actor="approver",
        )
        claim = claim_artifact_reconcile(session, operation.id, "my-node-1")

        assert case.classification == "local_only_bundle"
        assert claim["expected_ciphertext_digest"] == "d" * 64
        assert claim["expected_inventory_sequence"] == 0
        assert operation.status == "reconcile_artifact_running"
        assert operation.remote_call_state == "unknown"


def test_artifact_reconcile_can_resume_after_pre_bundle_lease_expiry() -> None:
    with _session() as session:
        operation = _seed_unknown_operation(session)
        case = preview_operation_reconcile(
            session,
            operation.id,
            tenant_id=1,
            expected_operation_version=operation.operation_version,
            evidence=_artifact_evidence(operation),
            actor="requester",
        )
        apply_operation_reconcile(
            session,
            operation.id,
            tenant_id=1,
            expected_operation_version=operation.operation_version,
            evidence_fingerprint=case.evidence_fingerprint,
            approval_ref="INC-ARTIFACT",
            idempotency_key="artifact-resume",
            actor="approver",
        )
        first = claim_artifact_reconcile(session, operation.id, "my-node-1")
        operation.lease_expires_at = _now() - timedelta(seconds=1)
        session.commit()

        second = claim_artifact_reconcile(session, operation.id, "my-node-1")

        assert second["owner_epoch"] == first["owner_epoch"] + 1
        assert second["lease_token"] != first["lease_token"]


def test_unrecoverable_legacy_artifact_closes_item_as_manual_required() -> None:
    with _session() as session:
        operation = _seed_unknown_operation(session)
        case = preview_operation_reconcile(
            session, operation.id, tenant_id=1, expected_operation_version=operation.operation_version,
            evidence=_artifact_evidence(operation), actor="reconcile-requester",
        )
        apply_operation_reconcile(
            session, operation.id, tenant_id=1, expected_operation_version=operation.operation_version,
            evidence_fingerprint=case.evidence_fingerprint, approval_ref="INC-ARTIFACT",
            idempotency_key="artifact-legacy", actor="reconcile-approver",
        )
        claim_artifact_reconcile(session, operation.id, "my-node-1")
        operation.lease_expires_at = _now() - timedelta(seconds=1)
        session.commit()
        preview = preview_artifact_abandon(
            session, operation.id, tenant_id=1, expected_operation_version=operation.operation_version,
            observed_ciphertext_digest="d" * 64, requested_by="abandon-requester",
        )

        result = apply_artifact_abandon(
            session, operation.id, tenant_id=1, expected_operation_version=operation.operation_version,
            observed_ciphertext_digest="d" * 64, requested_by="abandon-requester",
            evidence_fingerprint=preview["evidence_fingerprint"], actor="abandon-approver",
            approval_ref="INC-LEGACY-V1", idempotency_key="abandon-legacy-26",
        )
        item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)

        assert result["operation_status"] == "manual_required"
        assert result["blocker_code"] == "legacy_bundle_key_unrecoverable"
        assert item.status == "manual_required"
        assert item.outcome == "legacy_bundle_key_unrecoverable"
        assert operation.remote_call_state == "reconciled_hold"
        assert case.status == "applied"

        repeated = apply_artifact_abandon(
            session, operation.id, tenant_id=1, expected_operation_version=preview["operation_version"],
            observed_ciphertext_digest="d" * 64, requested_by="abandon-requester",
            evidence_fingerprint=preview["evidence_fingerprint"], actor="abandon-approver",
            approval_ref="INC-LEGACY-V1", idempotency_key="abandon-legacy-26",
        )
        assert repeated == result


def test_remote_orphan_closes_unknown_but_remains_protected_from_late_callback() -> None:
    with _session() as session:
        operation = _seed_unknown_operation(session)
        evidence = {
            "kind": "remote_orphan_without_bundle",
            "event_digest": "e" * 64,
            "source_ref": "production-readback:account-24",
            "runtime_image_sha": "a" * 40,
            "node_id": "my-node-1",
            "owner_epoch": 4,
            "remote_set_before_digest": "1" * 64,
            "remote_set_after_digest": "2" * 64,
            "new_device_count": 1,
        }
        case = preview_operation_reconcile(
            session, operation.id, tenant_id=1, expected_operation_version=1,
            evidence=evidence, actor="requester",
        )
        apply_operation_reconcile(
            session, operation.id, tenant_id=1, expected_operation_version=1,
            evidence_fingerprint=case.evidence_fingerprint, approval_ref="INC-ORPHAN",
            idempotency_key="orphan-24", actor="approver",
        )
        mark_login_remote_unknown(session, operation.id, node_id="my-node-1", owner_epoch=4)
        source = session.get(TgAccountAuthorization, 26)

        assert operation.status == "manual_required"
        assert operation.blocker_code == "orphan_remote_authorization_protected"
        assert operation.remote_call_state == "reconciled_hold"
        assert source.protected_from_cleanup is True


def test_confirmed_no_remote_effect_closes_unknown_as_failed() -> None:
    with _session() as session:
        operation = _seed_unknown_operation(session)
        evidence = {
            "kind": "confirmed_no_remote_effect",
            "event_digest": "e" * 64,
            "source_ref": "production-readback:account-67",
            "runtime_image_sha": "a" * 40,
            "node_id": "my-node-1",
            "owner_epoch": 4,
            "remote_set_before_digest": "1" * 64,
            "remote_set_after_digest": "1" * 64,
            "new_device_count": 0,
        }
        case = preview_operation_reconcile(
            session,
            operation.id,
            tenant_id=1,
            expected_operation_version=1,
            evidence=evidence,
            actor="requester",
        )

        apply_operation_reconcile(
            session,
            operation.id,
            tenant_id=1,
            expected_operation_version=1,
            evidence_fingerprint=case.evidence_fingerprint,
            approval_ref="INC-NO-EFFECT",
            idempotency_key="no-effect-67",
            actor="approver",
        )

        assert operation.status == "failed"
        assert operation.remote_call_state == "confirmed_no_effect"
        assert operation.blocker_code == "provision_confirmed_no_effect"


def test_pre_code_failure_reconcile_closes_b_unknown_without_changing_primary() -> None:
    with _session() as session:
        operation, primary, flow = _seed_b_pre_code_unknown(session)
        primary_before = (
            primary.session_ciphertext,
            primary.is_current,
            primary.is_slot_current,
            primary.fact_version,
        )
        evidence = build_pre_code_failure_evidence(
            session,
            operation.id,
            tenant_id=1,
            event_digest="e" * 64,
            source_ref="deploy-run:32523653620",
            runtime_image_sha="9" * 40,
        )
        case = preview_operation_reconcile(
            session,
            operation.id,
            tenant_id=1,
            expected_operation_version=operation.operation_version,
            evidence=evidence,
            actor="requester",
        )

        apply_operation_reconcile(
            session,
            operation.id,
            tenant_id=1,
            expected_operation_version=operation.operation_version,
            evidence_fingerprint=case.evidence_fingerprint,
            approval_ref="INC-PRE-CODE",
            idempotency_key="pre-code-account-2",
            actor="approver",
        )

        assert operation.status == "failed"
        assert operation.remote_call_state == "confirmed_no_effect"
        assert operation.blocker_code == "pre_code_submission_failure"
        assert operation.candidate_authorization_id is None
        assert case.classification == "confirmed_no_effect"
        assert flow.status == AccountStatus.ERROR.value
        assert flow.temporary_session_ciphertext is None
        assert flow.phone_code_hash_ciphertext is None
        assert (
            primary.session_ciphertext,
            primary.is_current,
            primary.is_slot_current,
            primary.fact_version,
        ) == primary_before


def test_artifact_recovery_reuses_original_bytes_and_adds_missing_snapshot(tmp_path) -> None:
    dek = AESGCM.generate_key(bit_length=256)
    envelope = _encrypt_session("original-session", dek, wrapped_dek=WrappedDek("wrapped", "key-v1"))
    local_path = tmp_path / "26" / "g2.bundle"
    local_path.parent.mkdir(parents=True)
    local_path.write_bytes(envelope)
    store = _MemoryStore()
    config = SimpleNamespace(
        local_dir=tmp_path,
        object_prefix="authorization-dr",
        object_store=store,
        dek_protector=_Protector(dek),
        snapshot_copy_kind="remote_ssh_snapshot",
    )
    claim = {
        "operation_id": "operation",
        "account_id": 26,
        "target_generation": 2,
        "developer_app_id": 3,
        "expected_ciphertext_digest": hashlib.sha256(envelope).hexdigest(),
        "expected_inventory_sequence": 0,
    }
    material = {"api_id": 103, "api_hash": "hash", "credentials_version": 1, "app_name": "C"}
    gateway = SimpleNamespace(authorization_identity=lambda *_args: _identity())

    receipt, object_key, identity = _recover_receipt(config, claim, material, gateway)

    assert store.read(object_key) == envelope
    assert receipt["ciphertext_digest"] == claim["expected_ciphertext_digest"]
    assert {item["copy_kind"] for item in receipt["copies"]} == {"local_persistent", "remote_ssh_snapshot"}
    assert receipt["inventory_sequence"] > 0
    assert identity == _identity()


def test_historical_phone_ban_projection_is_scoped_to_persisted_batch_fact() -> None:
    with _session() as session:
        operation = _seed_unknown_operation(session)
        operation.status = "failed"
        operation.remote_call_state = "confirmed_no_effect"
        operation.blocker_code = "phone_number_banned"
        session.commit()
        preview = preview_phone_ban_projection(session, 1, "batch")
        args = SimpleNamespace(
            tenant_id=1,
            batch_id="batch",
            actor="approver",
            requested_by="requester",
            approval_ref="INC-BAN-PROJECTION",
            idempotency_key="ban-projection-batch",
            expected_fingerprint=preview["fingerprint"],
        )

        readback = apply_phone_ban_projection(session, args)

        assert preview["confirmed_phone_banned_count"] == 1
        assert readback["already_projected_count"] == 1
        assert session.get(TgAccount, 26).status == AccountStatus.BANNED.value


def _seed_local_activate(session: Session):
    session.add(Tenant(id=1, name="tenant"))
    session.add_all([
        TelegramDeveloperApp(id=1, app_name="A", api_id=101, api_hash_ciphertext="a"),
        TelegramDeveloperApp(id=2, app_name="B", api_id=102, api_hash_ciphertext="b"),
        AccountProxy(id=1, tenant_id=1, name="p1", port=1),
        AccountProxy(id=2, tenant_id=1, name="p2", port=2),
    ])
    account = TgAccount(
        id=67,
        tenant_id=1,
        display_name="recover",
        phone_masked="67",
        developer_app_id=1,
        proxy_id=1,
        session_ciphertext=encrypt_session("old"),
        current_authorization_id=1,
        authorization_generation=4,
        authorization_fact_generation=7,
        connection_generation=9,
        status=AccountStatus.SESSION_EXPIRED.value,
    )
    primary = TgAccountAuthorization(
        id=1, tenant_id=1, account_id=67, role="primary", logical_slot="primary",
        is_current=True, is_slot_current=True, developer_app_id=1, proxy_id=1,
        session_ciphertext=encrypt_session("old"), status="active", health_status="expired",
    )
    target = TgAccountAuthorization(
        id=2, tenant_id=1, account_id=67, role="standby_1", logical_slot="standby_1",
        is_current=False, is_slot_current=True, provision_region_code="sv", developer_app_id=2,
        proxy_id=2, session_ciphertext=encrypt_session("standby"), status="standby",
        health_status="healthy", telegram_user_id_digest="b" * 64,
        auth_key_fingerprint_digest="a" * 64, fact_version=3,
    )
    session.add_all([account, primary, target, TgAccountOnlineState(tenant_id=1, account_id=67)])
    session.commit()
    return account, target


def _seed_unknown_operation(session: Session):
    session.add(Tenant(id=1, name="tenant"))
    session.add(AuthorizationDrRuntimeContract(id=1, mode="off"))
    session.add(AuthorizationDrExecutionNode(
        id="my-node-1", region_code="my", purpose="standby_session_dr", capability_version="2.20",
        runtime_image_sha="a" * 40, standby_egress_id="my-egress", status="ready", active_client_count=0,
        last_heartbeat_at=_now(),
    ))
    session.add(TelegramDeveloperApp(id=3, app_name="C", api_id=103, api_hash_ciphertext="c"))
    account = TgAccount(id=26, tenant_id=1, display_name="unknown", phone_masked="26")
    source = TgAccountAuthorization(
        id=26, tenant_id=1, account_id=26, role="standby_2", logical_slot="standby_2",
        slot_generation=1, is_slot_current=True, provision_region_code="sv", developer_app_id=3,
        session_ciphertext="source", status="standby", health_status="healthy", protected_from_cleanup=True,
    )
    batch = TgAuthorizationDrBatch(
        id="batch", tenant_id=1, idempotency_key="batch", target_set_fingerprint="f" * 64,
        target_count=1, status="reconcile_required", requested_by="requester",
    )
    item = TgAuthorizationDrBatchItem(
        id="item", batch_id="batch", tenant_id=1, account_id=26, ordinal=1,
        expected_source_authorization_id=26, expected_source_fact_version=1,
        expected_source_generation=1, target_generation=2, status="reconcile_unknown",
        outcome="provision_reconcile_unknown", blocker_code="provision_reconcile_unknown",
    )
    operation = TgAuthorizationDrOperation(
        id="operation", tenant_id=1, account_id=26, batch_item_id="item",
        operation_type="migrate_standby_2", logical_slot="standby_2", source_authorization_id=26,
        source_generation=1, target_generation=2, developer_app_id=3,
        developer_app_api_id_snapshot=103, developer_app_credentials_version=1, assignment_version=1,
        egress_id="my-egress", egress_version=1, idempotency_key="operation",
        request_fingerprint="e" * 64, status="provision_reconcile_unknown",
        remote_call_state="unknown", owner_node_id="my-node-1", owner_epoch=4,
        requested_by="requester", approved_by="approver", approval_ref="batch-approval",
    )
    item.operation_id = operation.id
    session.add_all([account, source, batch, operation, item])
    session.commit()
    return operation


def _seed_b_pre_code_unknown(session: Session):
    session.add(Tenant(id=1, name="tenant"))
    session.add(AuthorizationDrRuntimeContract(id=1, mode="off"))
    session.add(AuthorizationDrExecutionNode(
        id="my-node-1", region_code="my", purpose="standby_session_dr", capability_version="2.21",
        runtime_image_sha="9" * 40, standby_egress_id="my-egress", status="ready", active_client_count=0,
        last_heartbeat_at=_now(),
    ))
    session.add(TelegramDeveloperApp(id=2, app_name="B", api_id=102, api_hash_ciphertext="b"))
    account = TgAccount(
        id=2, tenant_id=1, display_name="canary", phone_masked="2", current_authorization_id=1,
        session_ciphertext=encrypt_session("primary"), authorization_generation=1,
        authorization_fact_generation=2, connection_generation=1,
    )
    primary = TgAccountAuthorization(
        id=1, tenant_id=1, account_id=2, role="primary", logical_slot="primary",
        is_current=True, is_slot_current=True, protected_from_cleanup=True,
        session_ciphertext=encrypt_session("primary"), status="active", health_status="healthy",
        fact_version=2,
    )
    flow = TgLoginFlow(
        id=22, tenant_id=1, account_id=2, method="code", status=AccountStatus.WAITING_CODE.value,
        challenge_sent_at=_now(), temporary_session_ciphertext="encrypted-temp",
        phone_code_hash_ciphertext="encrypted-hash", authorization_role="standby_1",
        developer_app_id=2,
    )
    operation = TgAuthorizationDrOperation(
        id="b-operation", tenant_id=1, account_id=2, operation_type="provision_standby_1",
        logical_slot="standby_1", source_authorization_id=1, code_source_authorization_id=1,
        source_generation=1, target_generation=1, developer_app_id=2,
        developer_app_api_id_snapshot=102, developer_app_credentials_version=1, assignment_version=1,
        egress_id="sv-proxy:1", egress_version=1, idempotency_key="b-operation",
        request_fingerprint="e" * 64, status="reconcile_unknown", remote_call_state="unknown",
        blocker_code="AuthKeyUnregisteredError", login_flow_id=22,
        login_code_message_id="233", login_code_received_at=_now(),
        requested_by="requester", approved_by="approver", approval_ref="canary",
    )
    session.add_all([account, primary, flow, operation])
    session.commit()
    return operation, primary, flow


def _artifact_evidence(operation) -> dict:
    return {
        "kind": "artifact_forward_recovery",
        "event_digest": "e" * 64,
        "source_ref": "production-readback:account-26",
        "runtime_image_sha": "a" * 40,
        "node_id": operation.owner_node_id,
        "owner_epoch": operation.owner_epoch,
        "bundle_generation": 2,
        "ciphertext_digest": "d" * 64,
        "inventory_sequence": 0,
    }


class _Protector:
    key_ref = "key-v1"

    def __init__(self, dek: bytes):
        self.dek = dek

    def unwrap(self, ciphertext: str) -> bytes:
        assert ciphertext == "wrapped"
        return self.dek


class _MemoryStore:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def exists(self, key: str) -> bool:
        return key in self.objects

    def read(self, key: str) -> bytes:
        return self.objects[key]

    def put_immutable(self, key: str, payload: bytes) -> str:
        if key in self.objects:
            raise RuntimeError("object exists")
        self.objects[key] = payload
        return f"version-{len(self.objects)}"
