from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountProxy,
    DeveloperAppSlotAssignment,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgLoginFlow,
    TgAuthorizationDrOperation,
)
from app.services._common import _now
from app.services.authorization_dr import apply_abc_backup, preview_abc_backup


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
            authorization_hash="987654",
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
