from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import AuthorizationIdentity, SendResult
from app.models import (
    AccountProxy,
    AccountStatus,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgAccountOnlineState,
)
from app.security import encrypt_session
from app.services._common import _now
from app.services.account_online_probe import OnlineProbeResult, _apply_probe_result
from app.services.authorization_dr import (
    apply_authkey_duplicate_projection,
    apply_local_activate,
    apply_local_activate_verification,
    preview_authkey_duplicate_projection,
    preview_local_activate,
    preview_local_activate_verification,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _identity() -> AuthorizationIdentity:
    return AuthorizationIdentity("123", "a" * 64, "b" * 64, "c" * 64)


def _seed(session: Session):
    session.add(Tenant(id=1, name="tenant"))
    session.add_all([
        TelegramDeveloperApp(id=1, app_name="A", api_id=101, api_hash_ciphertext="a"),
        TelegramDeveloperApp(id=2, app_name="B", api_id=102, api_hash_ciphertext="b"),
        AccountProxy(id=1, tenant_id=1, name="p1", port=1),
        AccountProxy(id=2, tenant_id=1, name="p2", port=2),
    ])
    account = TgAccount(
        id=8,
        tenant_id=1,
        display_name="recover",
        phone_masked="8",
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
        id=1,
        tenant_id=1,
        account_id=8,
        role="primary",
        logical_slot="primary",
        is_current=True,
        is_slot_current=True,
        developer_app_id=1,
        proxy_id=1,
        session_ciphertext=encrypt_session("old"),
        status="active",
        health_status="expired",
    )
    target = TgAccountAuthorization(
        id=2,
        tenant_id=1,
        account_id=8,
        role="standby_1",
        logical_slot="standby_1",
        is_current=False,
        is_slot_current=True,
        provision_region_code="sv",
        developer_app_id=2,
        proxy_id=2,
        session_ciphertext=encrypt_session("standby"),
        status="standby",
        health_status="healthy",
        telegram_user_id_digest="b" * 64,
        auth_key_fingerprint_digest="a" * 64,
        fact_version=3,
    )
    state = TgAccountOnlineState(tenant_id=1, account_id=8, online_status="login_required")
    session.add_all([account, primary, target, state])
    session.commit()
    return account, primary, target, state


def _activate(session: Session, monkeypatch):
    account, primary, target, state = _seed(session)
    monkeypatch.setattr(
        "app.services.authorization_dr.local_activate.credentials_for_developer_app",
        lambda *_args: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "app.services.authorization_dr.local_activate.credentials_for_authorization",
        lambda *_args: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "app.services.authorization_dr.local_activate.gateway.authorization_identity",
        lambda *_args: _identity(),
    )
    monkeypatch.setattr(
        "app.services.authorization_dr.local_activate.gateway.invalidate_session_cache",
        lambda *_args: 1,
    )
    case = preview_local_activate(session, 1, 8, 2, actor="requester", reason="typed A failure")
    apply_local_activate(
        session,
        1,
        8,
        2,
        fingerprint=case.fingerprint,
        actor="approver",
        approval_ref="INC-8",
        idempotency_key="activate-8",
    )
    session.refresh(account)
    session.refresh(case)
    return account, primary, target, state, case


def test_local_activate_holds_business_until_send_readback(monkeypatch) -> None:
    with _session() as session:
        account, _primary, target, state, case = _activate(session, monkeypatch)
        assert case.status == "applied_pending_verification"
        assert account.current_authorization_id == target.id
        assert account.status == AccountStatus.NEED_RELOGIN.value
        assert state.online_status == "recovering"

        preview = preview_local_activate_verification(
            session,
            1,
            8,
            case.id,
            idempotency_key="activate-8-verify",
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.local_activate_verify.credentials_for_authorization",
            lambda *_args: SimpleNamespace(),
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.local_activate_verify.gateway.send_message",
            lambda *_args: SendResult(True, "101", remote_mutation_started=True),
        )
        result = apply_local_activate_verification(
            session,
            1,
            8,
            case.id,
            idempotency_key="activate-8-verify",
            expected_fingerprint=preview["fingerprint"],
            requested_by="requester",
            approved_by="approver",
            approval_ref="INC-8",
        )

        session.refresh(account)
        session.refresh(case)
        session.refresh(state)
        assert result["status"] == "succeeded"
        assert result["primary_saved_message_id"] == "101"
        assert case.status == "applied"
        assert account.status == AccountStatus.ACTIVE.value
        assert account.business_runtime_status == "degraded"
        assert state.online_status == "warming"


def test_local_activate_send_unknown_keeps_new_primary_frozen(monkeypatch) -> None:
    with _session() as session:
        account, _primary, target, _state, case = _activate(session, monkeypatch)
        preview = preview_local_activate_verification(
            session,
            1,
            8,
            case.id,
            idempotency_key="activate-8-verify-unknown",
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.local_activate_verify.credentials_for_authorization",
            lambda *_args: SimpleNamespace(),
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.local_activate_verify.gateway.send_message",
            lambda *_args: (_ for _ in ()).throw(TimeoutError("lost response")),
        )
        result = apply_local_activate_verification(
            session,
            1,
            8,
            case.id,
            idempotency_key="activate-8-verify-unknown",
            expected_fingerprint=preview["fingerprint"],
            requested_by="requester",
            approved_by="approver",
            approval_ref="INC-8",
        )

        session.refresh(account)
        session.refresh(case)
        assert result["status"] == "reconcile_unknown"
        assert account.current_authorization_id == target.id
        assert account.status == AccountStatus.NEED_RELOGIN.value
        assert case.status == "verification_unknown"


def test_online_probe_projects_authkey_duplicate_to_current_authorization() -> None:
    with _session() as session:
        account, primary, _target, state = _seed(session)
        _apply_probe_result(
            session,
            account,
            state,
            _now(),
            OnlineProbeResult(account_id=8, error=type("AuthKeyDuplicatedError", (Exception,), {})()),
        )

        assert primary.health_status == "invalid"
        assert primary.dr_state == "invalid"
        assert primary.last_authoritative_error_code == "authorization_key_duplicated"
        assert account.authorization_fact_generation == 8

        _apply_probe_result(
            session,
            account,
            state,
            _now(),
            OnlineProbeResult(account_id=8, error=type("AuthKeyDuplicatedError", (Exception,), {})()),
        )
        assert primary.fact_version == 2
        assert account.authorization_fact_generation == 8


def test_authkey_duplicate_backfill_requires_typed_online_fact() -> None:
    with _session() as session:
        account, primary, _target, state = _seed(session)
        state.failure_detail = "AuthKeyDuplicatedError: duplicated"
        state.last_probe_at = _now()
        session.commit()
        preview = preview_authkey_duplicate_projection(session, 1, [8])

        result = apply_authkey_duplicate_projection(
            session,
            1,
            [8],
            expected_fingerprint=preview["fingerprint"],
            actor="approver",
            approval_ref="INC-8",
        )

        assert result["status"] == "projected"
        assert primary.health_status == "invalid"
        assert primary.last_authoritative_error_code == "authorization_key_duplicated"
        assert account.authorization_fact_generation == 8
