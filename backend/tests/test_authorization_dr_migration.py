from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    DeveloperAppSlotAssignment,
    TelegramDeveloperApp,
    TelegramEgressAssignment,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
    TgAuthorizationDrReconcileCase,
    TgAuthorizationWakeBundleCopy,
)
from app.services._common import _now
from app.services.authorization_dr import (
    AuthorizationDrError,
    CopyReceipt,
    RestoreProbeReceipt,
    WakeBundleReceipt,
    approve_migration_batch,
    apply_operation_reconcile,
    claim_migration_operation,
    commit_migration_slot,
    commit_wake_bundle_receipt,
    mark_login_remote_failed,
    mark_login_remote_started,
    mark_login_remote_unknown,
    migration_login_material,
    poll_migration_login_code,
    preview_migration_batch,
    preview_operation_reconcile,
    record_restore_probe,
    renew_migration_lease,
    rollback_migration_slot,
)
from app.services.developer_apps import (
    assign_developer_app_round_robin,
    list_developer_apps,
    update_developer_app_slot_assignments,
)


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed_runtime(db)
        yield db


def _seed_runtime(session: Session) -> None:
    session.add(Tenant(id=1, name="DR test tenant"))
    session.add_all([
        TelegramDeveloperApp(id=1, app_name="App A", api_id=1001, api_hash_ciphertext="a", credentials_version=1),
        TelegramDeveloperApp(id=2, app_name="App B", api_id=1002, api_hash_ciphertext="b", credentials_version=1),
        TelegramDeveloperApp(id=3, app_name="App C", api_id=1003, api_hash_ciphertext="c", credentials_version=1),
    ])
    session.flush()
    for purpose, app_id in (("primary_sv", 1), ("standby_1_sv", 2), ("standby_2_my", 3)):
        session.add(DeveloperAppSlotAssignment(
            slot_purpose=purpose,
            developer_app_id=app_id,
            assignment_version=7,
            credentials_version=1,
            assigned_by="platform-admin",
        ))
    session.add(AuthorizationDrRuntimeContract(id=1, mode="migrate", contract_epoch=3))
    session.add(TelegramEgressAssignment(
        id="my-egress-1",
        purpose="standby_my",
        region_code="my",
        secret_ref_digest="1" * 64,
        observed_ip_hmac="2" * 64,
        status="active",
        connectivity_status="verified",
        version=5,
        last_verified_at=_now(),
    ))
    session.add(AuthorizationDrExecutionNode(
        id="my-node-1",
        region_code="my",
        purpose="standby_session_dr",
        capability_version="2.16",
        standby_egress_id="my-egress-1",
        status="ready",
        active_client_count=0,
        last_heartbeat_at=_now(),
    ))
    _seed_account(session, 101)
    _seed_account(session, 102)
    session.commit()


def _seed_account(session: Session, account_id: int) -> None:
    session.add(TgAccount(
        id=account_id,
        tenant_id=1,
        display_name=f"account-{account_id}",
        phone_masked=str(account_id),
        session_ciphertext=f"primary-{account_id}",
        developer_app_id=1,
    ))
    session.flush()
    session.add_all([
        TgAccountAuthorization(
            tenant_id=1,
            account_id=account_id,
            role="standby_1",
            logical_slot="standby_1",
            provision_region_code="sv",
            developer_app_id=2,
            developer_app_api_id_snapshot=1002,
            session_ciphertext=f"sv-standby-1-{account_id}",
            status="standby",
            health_status="healthy",
            remote_authorization_state="active",
        ),
        TgAccountAuthorization(
            tenant_id=1,
            account_id=account_id,
            role="standby_2",
            logical_slot="standby_2",
            slot_generation=4,
            is_slot_current=True,
            provision_region_code="sv",
            credential_storage_scope="central_business",
            developer_app_id=3,
            developer_app_api_id_snapshot=1003,
            session_ciphertext=f"sv-standby-2-{account_id}",
            status="standby",
            health_status="healthy",
            dr_state="legacy_sv",
            remote_authorization_state="active",
            fact_version=6,
        ),
    ])


def _approved_batch(session: Session):
    batch = preview_migration_batch(
        session,
        1,
        [102, 101, 101],
        idempotency_key="canary-2",
        actor="requester",
    )
    return approve_migration_batch(
        session,
        batch.id,
        expected_version=batch.version,
        approval_ref="OPS-20260820-DR-CANARY",
        actor="reviewer",
    )


def _copy(kind: str, digest: str) -> CopyReceipt:
    current = _now()
    return CopyReceipt(
        copy_kind=kind,
        object_ref_digest=("a" if kind == "local_persistent" else "b") * 64,
        ciphertext_digest=digest,
        immutable_version=f"immutable-{kind}-v1",
        write_receipt_digest="c" * 64,
        readback_receipt_digest="d" * 64,
        write_verified_at=current,
        readback_verified_at=current,
        decrypt_verified_at=current,
    )


def _bundle_receipt(claim, *, copy_kinds=("local_persistent", "remote_ssh_snapshot")) -> WakeBundleReceipt:
    digest = "e" * 64
    return WakeBundleReceipt(
        bundle_generation=claim.target_generation,
        ciphertext_digest=digest,
        wrapped_dek_ciphertext="kms-wrapped-dek-ciphertext",
        kms_key_ref_digest="f" * 64,
        kms_key_version="kms-key-v3",
        auth_key_fingerprint_digest=f"{claim.account_id:064x}",
        telegram_user_id_digest=f"{claim.account_id + 1000:064x}",
        authorization_fingerprint_digest="2" * 64,
        remote_authorization_hash_ciphertext=f"encrypted-hash-{claim.account_id}",
        inventory_sequence=claim.account_id,
        inventory_manifest_digest="1" * 64,
        copies=tuple(_copy(kind, digest) for kind in copy_kinds),
    )


def _probe_receipt() -> RestoreProbeReceipt:
    return RestoreProbeReceipt(
        probe_generation=1,
        source_copy_kind="remote_ssh_snapshot",
        status="passed",
        session_parse_status="passed",
        authorization_status="authorized",
        identity_match_status="matched",
        auth_key_match_status="matched",
        source_client_disconnected=True,
        probe_client_disconnected=True,
        zeroize_receipt_digest="9" * 64,
    )


def _start_claim(session: Session):
    _approved_batch(session)
    claim = claim_migration_operation(session, "my-node-1")
    assert claim is not None
    mark_login_remote_started(
        session,
        claim.operation_id,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
    )
    return claim


def _commit_bundle_and_probe(session: Session, claim):
    bundle = commit_wake_bundle_receipt(
        session,
        claim.operation_id,
        _bundle_receipt(claim),
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
    )
    record_restore_probe(
        session,
        claim.operation_id,
        _probe_receipt(),
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
    )
    return bundle


def test_preview_freezes_exact_two_accounts_and_requires_separate_approver(session: Session) -> None:
    batch = preview_migration_batch(
        session,
        1,
        [102, 101, 101],
        idempotency_key="scope-test",
        actor="same-person",
    )
    items = list(session.scalars(select(TgAuthorizationDrBatchItem).where(
        TgAuthorizationDrBatchItem.batch_id == batch.id,
    ).order_by(TgAuthorizationDrBatchItem.ordinal)))

    assert batch.target_count == 2
    assert [item.account_id for item in items] == [101, 102]
    assert all(item.expected_source_fact_version == 6 for item in items)
    with pytest.raises(AuthorizationDrError, match="Approver must differ") as error:
        approve_migration_batch(
            session,
            batch.id,
            expected_version=batch.version,
            approval_ref="approval",
            actor="same-person",
        )
    assert error.value.code == "approval_actor_conflict"


def test_preview_rejects_account_without_healthy_sv_standby_1(session: Session) -> None:
    standby = session.scalar(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == 101,
        TgAccountAuthorization.logical_slot == "standby_1",
    ))
    standby.health_status = "failed"
    session.commit()

    with pytest.raises(AuthorizationDrError) as error:
        preview_migration_batch(session, 1, [101], idempotency_key="missing-sv-backup", actor="requester")

    assert error.value.code == "sv_redundancy_incomplete"


def test_global_my_owner_claims_only_one_canary_operation(session: Session) -> None:
    _approved_batch(session)

    first = claim_migration_operation(session, "my-node-1")
    second = claim_migration_operation(session, "my-node-1")

    assert first is not None
    assert second is None
    assert first.account_id == 101


def test_node_can_heartbeat_without_claiming_while_runtime_is_off(session: Session) -> None:
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    contract.mode = "off"
    session.commit()

    assert claim_migration_operation(session, "my-node-1") is None


def test_claim_exposes_frozen_login_material_and_renews_lease(session: Session, monkeypatch) -> None:
    claim = _start_claim(session)
    before = claim.lease_expires_at
    code_source_calls: list[tuple[int, str, int]] = []
    material = migration_login_material(
        session,
        claim.operation_id,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
    )
    operation = renew_migration_lease(
        session,
        claim.operation_id,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
    )

    monkeypatch.setattr(
        "app.services.authorization_dr.migration.gateway.poll_verification_codes",
        lambda account_id, *, session_ciphertext, credentials: (
            code_source_calls.append((account_id, session_ciphertext, credentials.app_id))
            or [SimpleNamespace(code="12345")]
        ),
    )
    code = poll_migration_login_code(
        session,
        claim.operation_id,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
    )

    assert material["api_id"] == 1003
    assert material["phone"] == str(claim.account_id)
    assert material["api_hash"]
    assert operation.lease_expires_at >= before
    assert code == "12345"
    assert code_source_calls == [(claim.account_id, f"sv-standby-2-{claim.account_id}", 3)]


def test_one_copy_cannot_commit_and_old_sv_session_is_preserved(session: Session) -> None:
    claim = _start_claim(session)
    source = session.scalar(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == claim.account_id,
        TgAccountAuthorization.logical_slot == "standby_2",
        TgAccountAuthorization.provision_region_code == "sv",
    ))
    old_ciphertext = source.session_ciphertext

    with pytest.raises(AuthorizationDrError) as error:
        commit_wake_bundle_receipt(
            session,
            claim.operation_id,
            _bundle_receipt(claim, copy_kinds=("local_persistent",)),
            node_id=claim.owner_node_id,
            owner_epoch=claim.owner_epoch,
            lease_token=claim.lease_token,
        )

    assert error.value.code == "wake_bundle_copy_count_insufficient"
    session.refresh(source)
    assert source.is_slot_current is True
    assert source.session_ciphertext == old_ciphertext


def test_matching_wake_bundle_receipt_replay_returns_existing_bundle(session: Session) -> None:
    claim = _start_claim(session)
    receipt = _bundle_receipt(claim)
    first = commit_wake_bundle_receipt(
        session,
        claim.operation_id,
        receipt,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
    )

    second = commit_wake_bundle_receipt(
        session,
        claim.operation_id,
        receipt,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
    )

    assert second.id == first.id


def test_restore_probe_is_required_before_slot_commit(session: Session) -> None:
    claim = _start_claim(session)
    commit_wake_bundle_receipt(
        session,
        claim.operation_id,
        _bundle_receipt(claim),
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
    )

    with pytest.raises(AuthorizationDrError) as error:
        commit_migration_slot(
            session,
            claim.operation_id,
            node_id=claim.owner_node_id,
            owner_epoch=claim.owner_epoch,
            lease_token=claim.lease_token,
        )

    assert error.value.code == "wake_bundle_restore_probe_failed"


def test_cutover_preserves_old_sv_session_and_enables_next_account(session: Session) -> None:
    claim = _start_claim(session)
    bundle = _commit_bundle_and_probe(session, claim)
    decision = commit_migration_slot(
        session,
        claim.operation_id,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
    )

    rows = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == claim.account_id,
        TgAccountAuthorization.logical_slot == "standby_2",
    ).order_by(TgAccountAuthorization.slot_generation)))
    source, candidate = rows
    copies = list(session.scalars(select(TgAuthorizationWakeBundleCopy).where(
        TgAuthorizationWakeBundleCopy.bundle_id == bundle.id,
    )))
    next_claim = claim_migration_operation(session, "my-node-1")

    assert source.status == "retained"
    assert source.session_ciphertext == f"sv-standby-2-{claim.account_id}"
    assert source.protected_from_cleanup is True
    assert source.migration_recovery_gate_status == "passed"
    assert candidate.session_ciphertext is None
    assert candidate.credential_storage_scope == "malaysia_wake_bundle"
    assert candidate.telegram_login_at is not None
    assert candidate.is_slot_current is True
    assert candidate.dr_state == "dormant_ready"
    assert bundle.is_active is True
    assert len(copies) == 2
    assert decision.recovery_gate_status == "passed"
    assert next_claim is not None and next_claim.account_id == 102


def test_remote_unknown_is_not_retried_and_next_account_can_continue(session: Session) -> None:
    claim = _start_claim(session)

    mark_login_remote_unknown(
        session,
        claim.operation_id,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
    )

    next_claim = claim_migration_operation(session, "my-node-1")
    assert next_claim is not None
    assert next_claim.account_id == 102


def test_phone_banned_is_terminal_and_next_account_can_continue(session: Session) -> None:
    claim = _start_claim(session)

    operation = mark_login_remote_failed(
        session,
        claim.operation_id,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
        blocker_code="phone_number_banned",
    )

    item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    source = session.get(TgAccountAuthorization, operation.source_authorization_id)
    next_claim = claim_migration_operation(session, "my-node-1")

    assert operation.status == "failed"
    assert operation.remote_call_state == "confirmed_no_effect"
    assert operation.blocker_code == "phone_number_banned"
    assert operation.finished_at is not None
    assert item.status == "failed"
    assert item.outcome == "phone_number_banned"
    assert item.finished_at is not None
    assert source.session_ciphertext == f"sv-standby-2-{claim.account_id}"
    assert source.is_slot_current is True
    assert next_claim is not None and next_claim.account_id == 102


def test_invalid_two_fa_is_manual_required_and_next_account_can_continue(session: Session) -> None:
    claim = _start_claim(session)

    operation = mark_login_remote_failed(
        session,
        claim.operation_id,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
        blocker_code="two_fa_invalid",
    )

    item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    source = session.get(TgAccountAuthorization, operation.source_authorization_id)
    next_claim = claim_migration_operation(session, "my-node-1")

    assert operation.status == "manual_required"
    assert operation.remote_call_state == "confirmed_no_effect"
    assert item.status == "manual_required"
    assert item.outcome == "two_fa_invalid"
    assert source.session_ciphertext == f"sv-standby-2-{claim.account_id}"
    assert source.is_slot_current is True
    assert next_claim is not None and next_claim.account_id == 102


def test_remote_unknown_cannot_overwrite_confirmed_phone_banned(session: Session) -> None:
    claim = _start_claim(session)
    operation = mark_login_remote_failed(
        session,
        claim.operation_id,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
        blocker_code="phone_number_banned",
    )

    mark_login_remote_unknown(
        session,
        claim.operation_id,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
    )
    session.refresh(operation)

    assert operation.status == "failed"
    assert operation.remote_call_state == "confirmed_no_effect"
    assert operation.blocker_code == "phone_number_banned"


def test_batch_becomes_manual_required_when_all_items_are_remote_unknown(session: Session) -> None:
    session.autoflush = False
    first = _start_claim(session)
    mark_login_remote_unknown(
        session,
        first.operation_id,
        node_id=first.owner_node_id,
        owner_epoch=first.owner_epoch,
    )
    second = claim_migration_operation(session, "my-node-1")
    mark_login_remote_unknown(
        session,
        second.operation_id,
        node_id=second.owner_node_id,
        owner_epoch=second.owner_epoch,
    )
    batch = session.scalar(select(TgAuthorizationDrBatch))

    assert batch.status == "reconcile_required"
    assert batch.execution_finished_at is not None
    assert batch.finished_at is None


def test_guarded_reconcile_normalizes_typed_failure_without_login(session: Session) -> None:
    first = _start_claim(session)
    mark_login_remote_unknown(
        session,
        first.operation_id,
        node_id=first.owner_node_id,
        owner_epoch=first.owner_epoch,
    )
    second = claim_migration_operation(session, "my-node-1")
    mark_login_remote_unknown(
        session,
        second.operation_id,
        node_id=second.owner_node_id,
        owner_epoch=second.owner_epoch,
    )
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    contract.mode = "off"
    session.commit()
    operation = session.get(TgAuthorizationDrOperation, first.operation_id)
    source = session.get(TgAccountAuthorization, operation.source_authorization_id)
    original_session = source.session_ciphertext
    evidence = {
        "kind": "historical_typed_login_failure",
        "blocker_code": "two_fa_invalid",
        "event_digest": "d" * 64,
        "source_ref": "codex-rollout:test-evidence-one",
        "runtime_image_sha": "a" * 40,
        "node_id": first.owner_node_id,
        "owner_epoch": first.owner_epoch,
    }

    case = preview_operation_reconcile(
        session,
        operation.id,
        tenant_id=1,
        expected_operation_version=operation.operation_version,
        evidence=evidence,
        actor="reconcile-requester",
    )
    applied = apply_operation_reconcile(
        session,
        operation.id,
        tenant_id=1,
        expected_operation_version=operation.operation_version,
        evidence_fingerprint=case.evidence_fingerprint,
        approval_ref="INC-DR-2FA-HISTORY",
        idempotency_key="reconcile-first",
        actor="reconcile-approver",
    )
    repeated = apply_operation_reconcile(
        session,
        operation.id,
        tenant_id=1,
        expected_operation_version=operation.operation_version,
        evidence_fingerprint=case.evidence_fingerprint,
        approval_ref="INC-DR-2FA-HISTORY",
        idempotency_key="reconcile-first",
        actor="reconcile-approver",
    )
    session.refresh(operation)
    session.refresh(source)
    item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    batch = session.get(TgAuthorizationDrBatch, item.batch_id)

    assert applied.id == repeated.id
    assert operation.status == "manual_required"
    assert operation.remote_call_state == "confirmed_no_effect"
    assert operation.reconcile_status == "applied"
    assert item.status == "manual_required"
    assert item.outcome == "two_fa_invalid"
    assert source.session_ciphertext == original_session
    assert source.is_slot_current is True
    assert source.protected_from_cleanup is True
    assert batch.status == "reconcile_required"
    assert batch.finished_at is None

    mark_login_remote_unknown(
        session,
        operation.id,
        node_id=first.owner_node_id,
        owner_epoch=first.owner_epoch,
    )
    session.refresh(operation)
    assert operation.status == "manual_required"


def test_reconcile_apply_rejects_frozen_source_drift(session: Session) -> None:
    claim = _start_claim(session)
    mark_login_remote_unknown(
        session,
        claim.operation_id,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
    )
    session.get(AuthorizationDrRuntimeContract, 1).mode = "off"
    session.commit()
    operation = session.get(TgAuthorizationDrOperation, claim.operation_id)
    evidence = {
        "kind": "historical_typed_login_failure",
        "blocker_code": "two_fa_invalid",
        "event_digest": "e" * 64,
        "source_ref": "codex-rollout:test-evidence-drift",
        "runtime_image_sha": "b" * 40,
        "node_id": claim.owner_node_id,
        "owner_epoch": claim.owner_epoch,
    }
    case = preview_operation_reconcile(
        session,
        operation.id,
        tenant_id=1,
        expected_operation_version=operation.operation_version,
        evidence=evidence,
        actor="reconcile-requester",
    )
    source = session.get(TgAccountAuthorization, operation.source_authorization_id)
    source.fact_version += 1
    session.commit()

    with pytest.raises(AuthorizationDrError, match="Frozen reconciliation facts changed"):
        apply_operation_reconcile(
            session,
            operation.id,
            tenant_id=1,
            expected_operation_version=operation.operation_version,
            evidence_fingerprint=case.evidence_fingerprint,
            approval_ref="INC-DR-DRIFT",
            idempotency_key="reconcile-drift",
            actor="reconcile-approver",
        )
    assert session.scalar(select(TgAuthorizationDrReconcileCase)).status == "decision_ready"


def test_single_item_batch_succeeds_with_autoflush_disabled(session: Session) -> None:
    session.autoflush = False
    batch = preview_migration_batch(
        session,
        1,
        [101],
        idempotency_key="single-success",
        actor="requester",
    )
    approve_migration_batch(
        session,
        batch.id,
        expected_version=1,
        approval_ref="ticket-single-success",
        actor="approver",
    )
    claim = claim_migration_operation(session, "my-node-1")
    mark_login_remote_started(
        session,
        claim.operation_id,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
    )
    _commit_bundle_and_probe(session, claim)
    commit_migration_slot(
        session,
        claim.operation_id,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
    )
    session.refresh(batch)

    assert batch.status == "succeeded"
    assert batch.finished_at is not None


def test_forward_rollback_uses_higher_decision_and_keeps_my_candidate(session: Session) -> None:
    claim = _start_claim(session)
    _commit_bundle_and_probe(session, claim)
    first = commit_migration_slot(
        session,
        claim.operation_id,
        node_id=claim.owner_node_id,
        owner_epoch=claim.owner_epoch,
        lease_token=claim.lease_token,
    )

    rollback = rollback_migration_slot(
        session,
        claim.operation_id,
        actor="incident-operator",
        reason="object restore degraded after cutover",
    )
    rows = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == claim.account_id,
        TgAccountAuthorization.logical_slot == "standby_2",
    ).order_by(TgAccountAuthorization.slot_generation)))

    assert rollback.decision_generation > first.decision_generation
    assert rows[0].is_slot_current is True
    assert rows[0].session_ciphertext
    assert rows[1].is_slot_current is False
    assert rows[1].protected_from_cleanup is True


def test_stale_my_node_blocks_approval_before_operations_are_created(session: Session) -> None:
    node = session.get(AuthorizationDrExecutionNode, "my-node-1")
    node.last_heartbeat_at = _now() - timedelta(minutes=5)
    session.commit()
    batch = preview_migration_batch(
        session,
        1,
        [101, 102],
        idempotency_key="stale-node",
        actor="requester",
    )

    with pytest.raises(AuthorizationDrError) as error:
        approve_migration_batch(
            session,
            batch.id,
            expected_version=batch.version,
            approval_ref="OPS-STALE",
            actor="reviewer",
        )

    assert error.value.code == "malaysia_wake_unavailable"


def test_developer_app_roles_are_versioned_and_drive_new_primary_assignment(session: Session) -> None:
    payload = SimpleNamespace(
        app_a_id=1,
        app_b_id=2,
        app_c_id=3,
        expected_assignment_version=7,
    )

    apps = update_developer_app_slot_assignments(session, payload, "platform-admin")
    account = TgAccount(tenant_id=1, display_name="new", phone_masked="new")
    selected = assign_developer_app_round_robin(session, account)

    assert selected.id == 1
    assert account.developer_app_id == 1
    assert {item["slot_purpose"] for item in apps} == {"primary_sv", "standby_1_sv", "standby_2_my"}
    assert {item["assignment_version"] for item in apps} == {8}
    app_c = next(item for item in list_developer_apps(session) if item["id"] == 3)
    assert app_c["used_distinct_accounts"] == 2


def test_developer_app_roles_reject_duplicate_apps_and_stale_version(session: Session) -> None:
    duplicate = SimpleNamespace(app_a_id=1, app_b_id=1, app_c_id=3, expected_assignment_version=7)
    stale = SimpleNamespace(app_a_id=1, app_b_id=2, app_c_id=3, expected_assignment_version=6)

    with pytest.raises(ValueError, match="三个不同"):
        update_developer_app_slot_assignments(session, duplicate, "platform-admin")
    with pytest.raises(ValueError, match="版本已变化"):
        update_developer_app_slot_assignments(session, stale, "platform-admin")
