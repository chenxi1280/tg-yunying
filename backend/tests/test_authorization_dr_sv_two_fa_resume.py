from __future__ import annotations

import hashlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from telethon.errors import PasswordHashInvalidError

from app.database import Base
from app.integrations.telegram.contracts import AuthorizationIdentity
from app.models import (
    AccountProxy,
    AccountStatus,
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgAccountSecuritySnapshot,
    TgAuthorizationDrOperation,
    TgAuthorizationDrReconcileCase,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
    TgLoginFlow,
)
from app.services._common import _now
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.sv_two_fa_resume import (
    apply_sv_two_fa_resume,
    preview_sv_two_fa_resume,
)
from app.services.authorization_dr.online_abc_runner import _require_post_b_reconcile_resume


pytestmark = pytest.mark.no_postgres
RUNTIME_SHA = "a" * 40


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(session: Session):
    now = _now()
    tenant = Tenant(
        id=1, name="SV 2FA resume", fixed_two_fa_password_ciphertext="fixed-secret",
        fixed_two_fa_password_set_at=now, fixed_two_fa_password_set_by="admin",
    )
    session.add(tenant)
    session.add_all([
        TelegramDeveloperApp(id=1, app_name="App B", api_id=1001, api_hash_ciphertext="b"),
        TelegramDeveloperApp(id=2, app_name="App A", api_id=1002, api_hash_ciphertext="a"),
    ])
    account = TgAccount(
        id=262, tenant_id=1, display_name="account-262", phone_masked="262",
        session_ciphertext="a-session", developer_app_id=2,
        status=AccountStatus.ACTIVE.value,
        authorization_generation=1, authorization_fact_generation=1, connection_generation=1,
    )
    session.add(account)
    session.flush()
    primary = TgAccountAuthorization(
        tenant_id=1, account_id=262, role="primary", logical_slot="primary",
        provision_region_code="sv", developer_app_id=2, session_ciphertext="a-session",
        status="active", health_status="healthy", derived_status="healthy",
        is_current=True, is_slot_current=True, protected_from_cleanup=True,
        telegram_user_id_digest="1" * 64, auth_key_fingerprint_digest="2" * 64, fact_version=1,
    )
    session.add(primary)
    session.flush()
    account.current_authorization_id = primary.id
    security = TgAccountSecuritySnapshot(
        tenant_id=1, account_id=262, trusted_session_status="confirmed", two_fa_status="enabled",
        two_fa_password_ciphertext="managed-secret", two_fa_password_stored_at=now - timedelta(days=1),
    )
    flow = TgLoginFlow(
        tenant_id=1, account_id=262, method="code", status=AccountStatus.WAITING_CODE.value,
        authorization_role="standby_1", developer_app_id=1,
        temporary_session_ciphertext="temp-secret", phone_code_hash_ciphertext="phone-hash",
        challenge_sent_at=now,
    )
    session.add_all([security, flow])
    session.flush()
    operation = TgAuthorizationDrOperation(
        tenant_id=1, account_id=262, operation_type="provision_standby_1",
        logical_slot="standby_1", source_authorization_id=primary.id,
        code_source_authorization_id=primary.id, source_generation=1, target_generation=1,
        expected_current_authorization_id=primary.id, expected_authorization_generation=1,
        expected_authorization_fact_generation=1, expected_connection_generation=1,
        expected_code_source_fact_version=1, expected_code_source_user_id_digest="1" * 64,
        expected_code_source_auth_key_digest="2" * 64, developer_app_id=1,
        developer_app_api_id_snapshot=1001, developer_app_credentials_version=1,
        assignment_version=1, egress_id="primary_regular:direct", egress_version=1,
        idempotency_key="online-abc:262:b", request_fingerprint="f" * 64,
        status="reconcile_unknown", blocker_code="PasswordHashInvalidError",
        remote_call_state="unknown", login_flow_id=flow.id, requested_by="requester",
        approved_by="reviewer", approval_ref="approved",
    )
    session.add(operation)
    session.flush()
    batch = TgAuthorizationOnlineAbcBatch(
        id="batch-262", tenant_id=1, idempotency_key="batch-262", target_set_fingerprint="b" * 64,
        target_count=1, deployed_release_sha=RUNTIME_SHA, execution_release_sha=RUNTIME_SHA,
        selection_mode="all_online_accounts", status="stopped", requested_by="requester",
        approved_by="reviewer", approval_ref="approved",
    )
    item = TgAuthorizationOnlineAbcItem(
        id="item-262", batch_id=batch.id, tenant_id=1, account_id=262, ordinal=1,
        primary_authorization_id=primary.id, primary_fact_version=1,
        authorization_generation=1, authorization_fact_generation=1, connection_generation=1,
        primary_session_digest=hashlib.sha256(b"a-session").hexdigest(),
        app_b_id=1, app_b_credentials_version=1, app_b_assignment_purpose="standby_1_sv",
        app_b_assignment_version=1, proxy_id=None, source_c_fact_version=0,
        source_c_slot_generation=0, status="stopped", outcome="reconcile_unknown",
    )
    slot = TgAuthorizationOnlineAbcSlotResult(
        id="slot-262", batch_id=batch.id, item_id=item.id, tenant_id=1, account_id=262,
        logical_slot="standby_1", outcome="reconcile_unknown", operation_id=operation.id,
        blocker_code="PasswordHashInvalidError",
    )
    session.add_all([batch, item, slot, AuthorizationDrRuntimeContract(id=1, mode="off")])
    session.add(AuthorizationDrExecutionNode(
        id="my", region_code="my", purpose="standby_session_dr", capability_version="2.21",
        runtime_image_sha=RUNTIME_SHA, standby_egress_id="my", status="ready", active_client_count=0,
    ))
    session.commit()
    return account, primary, security, flow, operation


def _patch_common(monkeypatch) -> None:
    def decrypt(value):
        return {"temp-secret": "raw-temp", "phone-hash": "raw-hash",
                "fixed-secret": "fixed-password", "managed-secret": "old-password"}.get(value, value)

    monkeypatch.setattr("app.services.authorization_dr.sv_two_fa_resume.decrypt_secret", decrypt)
    monkeypatch.setattr(
        "app.services.authorization_dr.sv_two_fa_resume_commit.encrypt_session", lambda value: f"session:{value}",
    )
    monkeypatch.setattr(
        "app.services.authorization_dr.sv_two_fa_resume_commit.encrypt_secret", lambda value: f"secret:{value}",
    )
    monkeypatch.setattr("app.services.account_two_fa.encrypt_secret", lambda value: f"managed:{value}")
    monkeypatch.setattr(
        "app.services.authorization_dr.sv_two_fa_resume.credentials_for_developer_app",
        lambda *_: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "app.services.authorization_dr.sv_two_fa_resume.credentials_for_authorization",
        lambda *_: SimpleNamespace(),
    )
    remote = SimpleNamespace(
        authorization_hash="987654", is_current=False, api_id=1001,
        device_model="recovered", platform="test", date_created=_now(), date_active=_now(),
    )
    monkeypatch.setattr(
        "app.services.authorization_dr.sv_two_fa_resume.gateway.list_authorizations",
        lambda *_: [remote],
    )


def _identity() -> AuthorizationIdentity:
    return AuthorizationIdentity(
        authorization_hash="987654", auth_key_fingerprint_digest="3" * 64,
        telegram_user_id_digest="1" * 64, authorization_fingerprint_digest="4" * 64,
    )


def _preview(session, operation):
    return preview_sv_two_fa_resume(
        session, operation.id, tenant_id=1, runtime_image_sha=RUNTIME_SHA, requested_by="requester",
    )


def _apply(session, operation, fingerprint):
    return apply_sv_two_fa_resume(
        session, operation.id, tenant_id=1, runtime_image_sha=RUNTIME_SHA,
        requested_by="requester", actor="reviewer", approval_ref="USER-262",
        idempotency_key="resume-262", expected_fingerprint=fingerprint,
    )


def _a_fence(account, primary) -> tuple:
    return (
        account.current_authorization_id, account.session_ciphertext, account.developer_app_id,
        account.authorization_generation, account.authorization_fact_generation,
        account.connection_generation, primary.fact_version, primary.session_ciphertext,
    )


def test_preview_is_db_only_and_stable(monkeypatch) -> None:
    with _session() as session:
        _account, _primary, _security, _flow, operation = _seed(session)
        _patch_common(monkeypatch)
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.authorization_identity",
            lambda *_: pytest.fail("preview connected to Telegram"),
        )
        first = _preview(session, operation)
        second = _preview(session, operation)
        assert first == second
        assert "fixed-password" not in str(first)


def test_legacy_proxy_plan_cannot_override_frozen_direct_operation(monkeypatch) -> None:
    with _session() as session:
        _account, _primary, _security, _flow, operation = _seed(session)
        session.add(AccountProxy(
            id=62, tenant_id=1, name="legacy-plan", host="127.0.0.1", port=1080, status="healthy",
        ))
        item = session.get(TgAuthorizationOnlineAbcItem, "item-262")
        item.proxy_id = 62
        session.commit()
        _patch_common(monkeypatch)
        preview = _preview(session, operation)
        assert preview["planned_proxy_id"] == 62
        assert preview["proxy_id"] is None
        assert preview["operation_egress_id"] == "primary_regular:direct"


def test_apply_submits_fixed_password_once_and_preserves_a(monkeypatch) -> None:
    with _session() as session:
        account, primary, security, flow, operation = _seed(session)
        _patch_common(monkeypatch)
        calls = []
        identities = iter([RuntimeError("session is not authorized"), _identity()])

        def identity(*_args):
            value = next(identities)
            if isinstance(value, Exception):
                raise value
            return value

        def finish(code, password, **kwargs):
            calls.append((code, password, kwargs))
            return AccountStatus.ACTIVE.value, "raw-authorized"

        monkeypatch.setattr("app.services.authorization_dr.sv_two_fa_resume.gateway.authorization_identity", identity)
        monkeypatch.setattr("app.services.authorization_dr.sv_two_fa_resume.gateway.finish_login", finish)
        before = _a_fence(account, primary)
        result = _apply(session, operation, _preview(session, operation)["evidence_fingerprint"])
        session.refresh(account)
        session.refresh(primary)
        session.refresh(security)
        session.refresh(flow)
        asset = session.get(TgAccountAuthorization, result["candidate_authorization_id"])
        assert _a_fence(account, primary) == before
        assert len(calls) == 1 and calls[0][0:2] == (None, "fixed-password")
        assert calls[0][2]["flow_id"] == flow.id
        assert asset.session_ciphertext == "session:raw-authorized"
        assert (asset.logical_slot, asset.is_slot_current, asset.is_current) == ("standby_1", True, False)
        assert security.two_fa_password_ciphertext == "managed:fixed-password"
        assert flow.temporary_session_ciphertext is None
        assert (operation.status, operation.reconcile_status) == ("succeeded", "applied")
        item = session.get(TgAuthorizationOnlineAbcItem, "item-262")
        _require_post_b_reconcile_resume(session, item, {"b": operation, "c": None, "e4": None})


def test_authorized_temp_session_never_resubmits_password(monkeypatch) -> None:
    with _session() as session:
        account, primary, _security, _flow, operation = _seed(session)
        _patch_common(monkeypatch)
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.authorization_identity",
            lambda *_: _identity(),
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.finish_login",
            lambda *_args, **_kwargs: pytest.fail("authorized Session resubmitted password"),
        )
        before = _a_fence(account, primary)
        result = _apply(session, operation, _preview(session, operation)["evidence_fingerprint"])
        assert result["classification"] == "sv_two_fa_session_recovered"
        assert _a_fence(account, primary) == before


def test_repeated_apply_returns_same_committed_case_without_remote_call(monkeypatch) -> None:
    with _session() as session:
        _account, _primary, _security, _flow, operation = _seed(session)
        _patch_common(monkeypatch)
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.authorization_identity",
            lambda *_: _identity(),
        )
        fingerprint = _preview(session, operation)["evidence_fingerprint"]
        first = _apply(session, operation, fingerprint)
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.authorization_identity",
            lambda *_: pytest.fail("idempotent apply connected to Telegram"),
        )
        second = _apply(session, operation, fingerprint)
        assert second == first


def test_legacy_a_uses_frozen_operation_identity_without_writing_a(monkeypatch) -> None:
    with _session() as session:
        account, primary, _security, _flow, operation = _seed(session)
        primary.telegram_user_id_digest = ""
        primary.auth_key_fingerprint_digest = ""
        session.commit()
        _patch_common(monkeypatch)
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.authorization_identity",
            lambda *_: _identity(),
        )
        result = _apply(session, operation, _preview(session, operation)["evidence_fingerprint"])
        session.refresh(primary)
        assert (primary.telegram_user_id_digest, primary.auth_key_fingerprint_digest) == ("", "")
        assert _a_fence(account, primary)[0:6] == (primary.id, "a-session", 2, 1, 1, 1)
        item = session.get(TgAuthorizationOnlineAbcItem, "item-262")
        _require_post_b_reconcile_resume(session, item, {"b": operation, "c": None, "e4": None})
        assert result["candidate_authorization_id"] is not None


def test_invalid_fixed_password_is_typed_no_effect(monkeypatch) -> None:
    with _session() as session:
        account, primary, security, flow, operation = _seed(session)
        _patch_common(monkeypatch)
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.authorization_identity",
            lambda *_: (_ for _ in ()).throw(RuntimeError("session is not authorized")),
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.finish_login",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(PasswordHashInvalidError(request=None)),
        )
        before = _a_fence(account, primary)
        managed_before = security.two_fa_password_ciphertext
        result = _apply(session, operation, _preview(session, operation)["evidence_fingerprint"])
        session.refresh(flow)
        case = session.scalar(select(TgAuthorizationDrReconcileCase).where(
            TgAuthorizationDrReconcileCase.operation_id == operation.id,
        ))
        assert _a_fence(account, primary) == before
        assert security.two_fa_password_ciphertext == managed_before
        assert result["classification"] == "sv_two_fa_invalid"
        assert (operation.status, operation.remote_call_state) == ("manual_required", "confirmed_no_effect")
        assert case.persisted_artifact_state == "confirmed_no_effect"
        assert flow.temporary_session_ciphertext is None


def test_unknown_remote_error_keeps_original_operation_and_a(monkeypatch) -> None:
    with _session() as session:
        account, primary, _security, flow, operation = _seed(session)
        _patch_common(monkeypatch)
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.authorization_identity",
            lambda *_: (_ for _ in ()).throw(ConnectionError("lost")),
        )
        before = _a_fence(account, primary)
        fingerprint = _preview(session, operation)["evidence_fingerprint"]
        with pytest.raises(ConnectionError, match="lost"):
            _apply(session, operation, fingerprint)
        session.rollback()
        assert _a_fence(account, primary) == before
        assert (operation.status, operation.remote_call_state) == ("reconcile_unknown", "unknown")
        assert flow.temporary_session_ciphertext == "temp-secret"


def test_fingerprint_conflict_stops_before_remote(monkeypatch) -> None:
    with _session() as session:
        _account, _primary, _security, _flow, operation = _seed(session)
        _patch_common(monkeypatch)
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.authorization_identity",
            lambda *_: pytest.fail("fingerprint conflict connected to Telegram"),
        )
        preview = _preview(session, operation)
        operation.operation_version += 1
        session.commit()
        with pytest.raises(AuthorizationDrError) as exc_info:
            _apply(session, operation, preview["evidence_fingerprint"])
        assert exc_info.value.code == "reconcile_evidence_conflict"


def test_preview_rejects_another_global_unknown(monkeypatch) -> None:
    with _session() as session:
        _account, primary, _security, _flow, operation = _seed(session)
        _patch_common(monkeypatch)
        session.add(TgAuthorizationDrOperation(
            tenant_id=1, account_id=262, operation_type="migrate_standby_2",
            logical_slot="standby_2", source_authorization_id=primary.id,
            code_source_authorization_id=primary.id, source_generation=1, target_generation=2,
            developer_app_id=1, developer_app_api_id_snapshot=1001,
            developer_app_credentials_version=1, assignment_version=1,
            egress_id="primary_regular:direct", egress_version=1,
            idempotency_key="other-unknown", request_fingerprint="u" * 64,
            status="provision_reconcile_unknown", remote_call_state="unknown",
            requested_by="requester", approved_by="reviewer", approval_ref="approved",
        ))
        session.commit()
        with pytest.raises(AuthorizationDrError) as exc_info:
            _preview(session, operation)
        assert exc_info.value.code == "reconcile_transition_blocked"
