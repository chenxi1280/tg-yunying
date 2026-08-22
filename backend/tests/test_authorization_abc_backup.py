from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountProxy,
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    DeveloperAppSlotAssignment,
    TelegramDeveloperApp,
    TelegramEgressAssignment,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgLoginFlow,
    TgAuthorizationDrOperation,
    TgAuthorizationRestoreProbeFact,
    TgAuthorizationWakeBundle,
    TgAuthorizationWakeBundleCopy,
)
from app.integrations.telegram.contracts import AuthorizationIdentity, SendResult
from app.services._common import _now
from app.services.authorization_dr import (
    apply_abc_backup,
    apply_abc_e4,
    preview_abc_backup,
    preview_abc_e4,
)
from app.services.authorization_dr.abc_backup import _retain_conflicting_b


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed(db)
        yield db


def _seed(session: Session) -> None:
    session.add(Tenant(id=1, name="ABC test"))
    session.add_all([
        TelegramDeveloperApp(id=1, app_name="A", api_id=1001, api_hash_ciphertext="a"),
        TelegramDeveloperApp(id=2, app_name="B", api_id=1002, api_hash_ciphertext="b"),
        TelegramDeveloperApp(id=3, app_name="C", api_id=1003, api_hash_ciphertext="c"),
    ])
    session.flush()
    session.add(DeveloperAppSlotAssignment(
        slot_purpose="standby_1_sv",
        developer_app_id=2,
        assignment_version=7,
        credentials_version=1,
        assigned_by="admin",
    ))
    session.add(AccountProxy(
        id=8,
        tenant_id=1,
        name="sv",
        host="127.0.0.1",
        port=1080,
        status="healthy",
    ))
    account = TgAccount(
        id=101,
        tenant_id=1,
        display_name="abc",
        phone_masked="101",
        session_ciphertext="primary-session",
        developer_app_id=1,
        proxy_id=8,
        authorization_generation=4,
        authorization_fact_generation=5,
        connection_generation=6,
    )
    session.add(account)
    session.flush()
    primary = TgAccountAuthorization(
        tenant_id=1,
        account_id=101,
        role="primary",
        logical_slot="primary",
        provision_region_code="sv",
        developer_app_id=1,
        developer_app_api_id_snapshot=1001,
        proxy_id=8,
        session_ciphertext="primary-session",
        status="active",
        health_status="healthy",
        is_current=True,
        telegram_user_id_digest="1" * 64,
        auth_key_fingerprint_digest="2" * 64,
        fact_version=3,
    )
    session.add(primary)
    session.flush()
    account.current_authorization_id = primary.id
    session.commit()


def test_preview_is_database_only_and_freezes_a(session: Session) -> None:
    result = preview_abc_backup(session, 1, 101, idempotency_key="abc-101")

    assert result["primary_authorization_id"] == session.get(TgAccount, 101).current_authorization_id
    assert result["app_b_id"] == 2
    assert len(result["fingerprint"]) == 64
    assert session.scalar(select(TgLoginFlow.id)) is None


def test_preview_selects_other_sv_app_when_historical_primary_uses_app_b(session: Session) -> None:
    account = session.get(TgAccount, 101)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    session.add(DeveloperAppSlotAssignment(
        slot_purpose="primary_sv",
        developer_app_id=1,
        assignment_version=9,
        credentials_version=1,
        assigned_by="admin",
    ))
    account.developer_app_id = 2
    primary.developer_app_id = 2
    session.commit()

    result = preview_abc_backup(session, 1, 101, idempotency_key="abc-dynamic-sv-app")

    assert result["app_b_id"] == 1
    assert result["app_b_assignment_purpose"] == "primary_sv"
    assert result["assignment_version"] == 9
    assert session.scalar(select(TgLoginFlow.id)) is None


def test_new_dynamic_b_retains_same_app_historical_standby_without_changing_a(session: Session) -> None:
    account = session.get(TgAccount, 101)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    account.developer_app_id = 2
    primary.developer_app_id = 2
    conflict = TgAccountAuthorization(
        tenant_id=1, account_id=101, role="standby_1", logical_slot="standby_1",
        developer_app_id=2, session_ciphertext="old-b", status="standby",
        health_status="healthy", is_slot_current=True, protected_from_cleanup=True,
    )
    replacement = TgAccountAuthorization(
        tenant_id=1, account_id=101, role="standby_1", logical_slot="standby_1",
        developer_app_id=1, session_ciphertext="new-b", status="standby",
        health_status="healthy", is_slot_current=True, protected_from_cleanup=True,
    )
    session.add_all([conflict, replacement])
    session.commit()
    before = _a_snapshot(session)

    _retain_conflicting_b(session, replacement, primary)
    session.commit()

    assert _a_snapshot(session) == before
    assert (conflict.role, conflict.logical_slot, conflict.status) == (
        "standby_repair", "standby_repair", "needs_repair",
    )
    assert conflict.protected_from_cleanup is True
    assert replacement.logical_slot == "standby_1"


def test_apply_logs_in_b_without_changing_a(session: Session, monkeypatch) -> None:
    preview = preview_abc_backup(session, 1, 101, idempotency_key="abc-101")
    before = _a_snapshot(session)

    def fake_start(db, account_id, **_kwargs):
        flow = TgLoginFlow(
            tenant_id=1,
            account_id=account_id,
            method="code",
            status="等待验证码",
            authorization_role="standby_1",
            developer_app_id=2,
            proxy_id=8,
            challenge_sent_at=_now(),
            code_expires_at=_now().replace(year=_now().year + 1),
        )
        db.add(flow)
        db.commit()
        return flow

    def fake_verify(db, account_id, *_args, **_kwargs):
        asset = TgAccountAuthorization(
            tenant_id=1,
            account_id=account_id,
            role="standby_1",
            logical_slot="standby_1",
            developer_app_id=2,
            developer_app_api_id_snapshot=1002,
            proxy_id=8,
            session_ciphertext="b-session",
            status="standby",
            health_status="healthy",
            is_current=False,
        )
        db.add(asset)
        db.commit()
        return asset

    monkeypatch.setattr("app.services.authorization_dr.abc_backup.start_standby_authorization_login", fake_start)
    monkeypatch.setattr("app.services.authorization_dr.abc_backup.verify_standby_authorization_login", fake_verify)
    monkeypatch.setattr("app.services.authorization_dr.abc_backup.managed_two_fa_password", lambda *_: None)
    monkeypatch.setattr("app.services.authorization_dr.abc_backup.decrypt_session", lambda value: value)
    monkeypatch.setattr("app.services.authorization_dr.abc_backup.encrypt_secret", lambda value: f"enc:{value}")
    monkeypatch.setattr(
        "app.services.authorization_dr.abc_backup.gateway.poll_verification_codes",
        lambda *_args, **_kwargs: [SimpleNamespace(
            code="12345",
            message_id="777000:abc",
            received_at=_now(),
        )],
    )
    monkeypatch.setattr(
        "app.services.authorization_dr.abc_backup.gateway.authorization_identity",
        lambda *_args, **_kwargs: SimpleNamespace(
            telegram_user_id_digest="1" * 64,
            auth_key_fingerprint_digest="3" * 64,
            authorization_hash="0",
            authorization_fingerprint_digest="4" * 64,
        ),
    )
    monkeypatch.setattr(
        "app.services.authorization_dr.abc_backup.resolve_authorization_identity_hash",
        lambda _session, _account_id, identity, **_kwargs: (
            SimpleNamespace(**{**identity.__dict__, "authorization_hash": "987654"}),
            "peer_observer",
        ),
    )

    result = apply_abc_backup(
        session,
        1,
        101,
        idempotency_key="abc-101",
        expected_fingerprint=preview["fingerprint"],
        requested_by="requester",
        approved_by="reviewer",
        approval_ref="USER-ABC-101",
    )

    assert result["status"] == "succeeded"
    assert _a_snapshot(session) == before
    b = session.get(TgAccountAuthorization, result["candidate_authorization_id"])
    assert (b.logical_slot, b.provision_region_code, b.is_slot_current) == ("standby_1", "sv", True)
    assert b.telegram_user_id_digest == "1" * 64
    assert b.auth_key_fingerprint_digest == "3" * 64


def test_remote_start_failure_is_unknown_and_never_changes_a(session: Session, monkeypatch) -> None:
    preview = preview_abc_backup(session, 1, 101, idempotency_key="abc-fail-101")
    before = _a_snapshot(session)
    monkeypatch.setattr(
        "app.services.authorization_dr.abc_backup.start_standby_authorization_login",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("transport lost")),
    )

    with pytest.raises(RuntimeError, match="transport lost"):
        apply_abc_backup(
            session,
            1,
            101,
            idempotency_key="abc-fail-101",
            expected_fingerprint=preview["fingerprint"],
            requested_by="requester",
            approved_by="reviewer",
            approval_ref="USER-ABC-FAIL-101",
        )

    assert _a_snapshot(session) == before
    operation = session.scalar(select(TgAuthorizationDrOperation))
    assert operation.status == "reconcile_unknown"
    assert operation.candidate_authorization_id is None


def test_abc_e4_sends_once_and_preserves_a(session: Session, monkeypatch) -> None:
    _seed_e4(session)
    preview = preview_abc_e4(session, 1, 101, idempotency_key="abc-e4-101")
    before = _a_snapshot(session)
    account = session.get(TgAccount, 101)
    standby = session.scalar(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == 101,
        TgAccountAuthorization.logical_slot == "standby_1",
    ))
    resolved_ids = []
    monkeypatch.setattr(
        "app.services.authorization_dr.abc_verify.gateway.send_message",
        lambda *_args, **_kwargs: SendResult(True, remote_message_id="9001", remote_mutation_started=True),
    )

    def identity(raw_session, _credentials):
        digest = "2" * 64 if raw_session == "primary-session" else "3" * 64
        return AuthorizationIdentity("0", digest, "1" * 64, "4" * 64)

    def resolve_identity(_session, _account_id, value, *, exclude_authorization_id):
        resolved_ids.append(exclude_authorization_id)
        return AuthorizationIdentity(
            "123", value.auth_key_fingerprint_digest,
            value.telegram_user_id_digest, value.authorization_fingerprint_digest,
        ), "peer_observer"

    monkeypatch.setattr("app.services.authorization_dr.abc_verify.gateway.authorization_identity", identity)
    monkeypatch.setattr(
        "app.services.authorization_dr.abc_verify.resolve_authorization_identity_hash",
        resolve_identity,
    )

    result = apply_abc_e4(
        session,
        1,
        101,
        idempotency_key="abc-e4-101",
        expected_fingerprint=preview["fingerprint"],
        requested_by="requester",
        approved_by="reviewer",
        approval_ref="USER-ABC-E4-101",
    )

    assert result["status"] == "succeeded"
    assert result["primary_saved_message_id"] == "9001"
    assert resolved_ids == [account.current_authorization_id, standby.id]
    assert _a_snapshot(session) == before


def test_abc_e4_unknown_send_is_not_retried(session: Session, monkeypatch) -> None:
    _seed_e4(session)
    preview = preview_abc_e4(session, 1, 101, idempotency_key="abc-e4-unknown-101")
    before = _a_snapshot(session)
    calls = []

    def unknown_send(*_args, **_kwargs):
        calls.append("called")
        return SendResult(False, failure_type="transport_lost", remote_mutation_started=None)

    monkeypatch.setattr("app.services.authorization_dr.abc_verify.gateway.send_message", unknown_send)
    values = dict(
        idempotency_key="abc-e4-unknown-101",
        expected_fingerprint=preview["fingerprint"],
        requested_by="requester",
        approved_by="reviewer",
        approval_ref="USER-ABC-E4-UNKNOWN-101",
    )

    first = apply_abc_e4(session, 1, 101, **values)
    second = apply_abc_e4(session, 1, 101, **values)

    assert first["status"] == second["status"] == "reconcile_unknown"
    assert calls == ["called"]
    assert _a_snapshot(session) == before


def _seed_e4(session: Session) -> None:
    account = session.get(TgAccount, 101)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    standby = TgAccountAuthorization(
        tenant_id=1, account_id=101, role="standby_1", logical_slot="standby_1",
        provision_region_code="sv", developer_app_id=2, proxy_id=8,
        session_ciphertext="b-session", status="active", health_status="healthy",
        is_slot_current=True, telegram_user_id_digest="1" * 64,
        auth_key_fingerprint_digest="3" * 64,
    )
    malaysia = TgAccountAuthorization(
        tenant_id=1, account_id=101, role="standby_2", logical_slot="standby_2",
        provision_region_code="my", developer_app_id=3, status="active", health_status="healthy",
        is_slot_current=True, is_current=False, telegram_user_id_digest="1" * 64,
        auth_key_fingerprint_digest="4" * 64,
    )
    session.add_all([standby, malaysia])
    session.flush()
    operation = _seed_migration_operation(session, primary, malaysia)
    bundle = TgAuthorizationWakeBundle(
        tenant_id=1, account_id=101, authorization_id=malaysia.id, operation_id=operation.id,
        bundle_generation=1, ciphertext_digest="5" * 64, wrapped_dek_ciphertext="wrapped",
        kms_key_ref_digest="6" * 64, kms_key_version="v1", kms_decrypt_status="verified",
        auth_key_fingerprint_digest="4" * 64, telegram_user_id_digest="1" * 64,
        recoverable_copy_count=2, receipt_status="active", is_active=True,
    )
    session.add(bundle)
    session.flush()
    malaysia.wake_bundle_id = bundle.id
    _seed_bundle_evidence(session, bundle, operation)
    session.add_all([
        DeveloperAppSlotAssignment(
            slot_purpose="primary_sv", developer_app_id=1, assignment_version=7,
            credentials_version=1, assigned_by="admin",
        ),
        DeveloperAppSlotAssignment(
            slot_purpose="standby_2_my", developer_app_id=3, assignment_version=7,
            credentials_version=1, assigned_by="admin",
        ),
        TelegramEgressAssignment(
            id="my-egress-1", purpose="standby_my", region_code="my",
            secret_ref_digest="d" * 64, observed_ip_hmac="e" * 64,
            status="active", connectivity_status="verified", version=1,
            last_verified_at=_now(),
        ),
    ])
    session.add(AuthorizationDrRuntimeContract(id=1, mode="off", claim_scope_operation_id=""))
    session.add(AuthorizationDrExecutionNode(
        id="my-node-1", region_code="my", purpose="standby_session_dr",
        capability_version="2.21-abc-a-source", runtime_image_sha="7" * 40,
        standby_egress_id="my-egress-1", status="ready", active_client_count=0,
        last_heartbeat_at=_now(),
    ))
    session.commit()


def _seed_migration_operation(session, primary, malaysia):
    operation = TgAuthorizationDrOperation(
        tenant_id=1, account_id=101, operation_type="migrate_standby_2", logical_slot="standby_2",
        source_authorization_id=primary.id, code_source_authorization_id=primary.id,
        candidate_authorization_id=malaysia.id, source_generation=1, target_generation=1,
        developer_app_id=3, developer_app_api_id_snapshot=1003, developer_app_credentials_version=1,
        assignment_version=1, egress_id="my-egress-1", egress_version=1,
        idempotency_key="seed-c", request_fingerprint="8" * 64, status="succeeded",
        requested_by="seed", approved_by="seed-reviewer", approval_ref="seed-approved",
    )
    session.add(operation)
    session.flush()
    return operation


def _seed_bundle_evidence(session, bundle, operation) -> None:
    for copy_kind in ("my_local_volume", "my_remote_ssh_snapshot"):
        session.add(TgAuthorizationWakeBundleCopy(
            bundle_id=bundle.id, copy_kind=copy_kind, object_ref_digest="9" * 64,
            ciphertext_digest=bundle.ciphertext_digest, immutable_version="v1",
            write_receipt_digest="a" * 64, readback_receipt_digest="b" * 64,
            write_verified_at=_now(), readback_verified_at=_now(), decrypt_verified_at=_now(),
        ))
    session.add(TgAuthorizationRestoreProbeFact(
        bundle_id=bundle.id, operation_id=operation.id, probe_generation=1,
        source_copy_kind="my_remote_ssh_snapshot", status="passed", session_parse_status="passed",
        authorization_status="authorized", identity_match_status="matched", auth_key_match_status="matched",
        source_client_disconnected=True, probe_client_disconnected=True,
        zeroize_receipt_digest="c" * 64,
    ))


def _a_snapshot(session: Session) -> tuple:
    account = session.get(TgAccount, 101)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    return (
        account.current_authorization_id,
        account.session_ciphertext,
        account.developer_app_id,
        account.proxy_id,
        account.authorization_generation,
        account.authorization_fact_generation,
        account.connection_generation,
        primary.fact_version,
        primary.session_ciphertext,
    )
