from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram.contracts import AccountAuthorizationSnapshot
from app.models import (
    AccountEnvironmentBinding,
    AccountProxy,
    AuditLog,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgAccountAuthorizationSnapshot,
)
from app.security import encrypt_secret
from app.services import account_environment_observations as observation_service
from app.services.account_environment_observations import refresh_account_environment_observations


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    return session


def _seed_environment(session: Session) -> None:
    app = TelegramDeveloperApp(id=11, app_name="TG App A", api_id=10011, api_hash_ciphertext="enc", is_active=True)
    account = TgAccount(id=101, tenant_id=1, display_name="账号A", username="acct_a", phone_masked="***1234", status="在线")
    authorization = TgAccountAuthorization(
        id=201,
        tenant_id=1,
        account_id=101,
        role="primary",
        developer_app_id=11,
        developer_app_api_id_snapshot=10011,
        proxy_id=31,
        session_ciphertext="enc-session",
        status="active",
        is_current=True,
    )
    proxy = AccountProxy(id=31, tenant_id=1, name="节点A", protocol="socks5", host="127.0.0.1", port=1080, status="healthy", alert_status="normal")
    session.add_all([app, account, authorization, proxy])
    session.commit()


def _remote_snapshot(session_ciphertext: str, device: str) -> AccountAuthorizationSnapshot:
    return AccountAuthorizationSnapshot(
        authorization_hash=f"{session_ciphertext}-hash",
        is_current=True,
        device_model=device,
        platform="ios" if device.startswith("iPhone") else "android",
        system_version="iOS 17.5" if device.startswith("iPhone") else "Android 14",
        api_id=10011,
        app_name="Telegram",
        app_version="10.14.1",
        ip="203.0.113.10",
        country="SG",
        region="Singapore",
        date_created=datetime(2026, 7, 1),
        date_active=datetime(2026, 7, 4),
    )


def test_refresh_account_environment_observations_refreshes_remote_snapshots_and_audits(monkeypatch) -> None:
    with _session() as session:
        _seed_environment(session)
        calls: list[str] = []
        session.get(TgAccountAuthorization, 201).telegram_authorization_hash_ciphertext = encrypt_secret("enc-session-hash")
        _add_environment_binding(session, auth_id=201, role="primary", device="iPhone 15")

        def list_authorizations(session_ciphertext, _credentials):
            calls.append(session_ciphertext)
            return [_remote_snapshot(session_ciphertext, "iPhone 16")]

        _patch_gateway(monkeypatch, list_authorizations)

        rows = refresh_account_environment_observations(session, tenant_id=1, actor="tester")
        audit_log = session.scalar(select(AuditLog).where(AuditLog.action == "刷新授权环境远端观测"))

    assert len(rows) == 1
    assert calls == ["enc-session"]
    assert rows[0].observed_device_model == "iPhone 16"
    assert rows[0].observed_system_version == "iOS 17.5"
    assert rows[0].consistency_status == "observed_mismatch"
    assert audit_log is not None
    assert audit_log.actor == "tester"
    assert audit_log.detail == "authorization_slots=1; refreshed_slots=1; source=telegram_authorization_list"


def test_refresh_account_environment_observations_projects_by_authorization_scope_without_hash(monkeypatch) -> None:
    with _session() as session:
        _seed_environment(session)
        session.add(
            TgAccountAuthorization(
                id=202,
                tenant_id=1,
                account_id=101,
                role="standby_1",
                developer_app_id=11,
                developer_app_api_id_snapshot=10011,
                proxy_id=31,
                session_ciphertext="standby-session",
                status="standby",
            )
        )
        _add_environment_binding(session, auth_id=201, role="primary", device="iPhone 15")
        _add_environment_binding(session, auth_id=202, role="standby_1", device="Pixel 8")

        def list_authorizations(session_ciphertext, _credentials):
            device = "iPhone 15" if session_ciphertext == "enc-session" else "Pixel 8"
            other = "Pixel 8" if session_ciphertext == "enc-session" else "iPhone 15"
            return [
                _remote_snapshot(session_ciphertext, device),
                AccountAuthorizationSnapshot(
                    authorization_hash=f"other-{session_ciphertext}-hash",
                    is_current=False,
                    device_model=other,
                    platform="android" if other.startswith("Pixel") else "ios",
                    system_version="Android 14" if other.startswith("Pixel") else "iOS 17.5",
                    api_id=10011,
                    app_name="Telegram",
                    app_version="10.14.1",
                    ip="203.0.113.11",
                    country="SG",
                    region="Singapore",
                    date_created=datetime(2026, 7, 1),
                    date_active=datetime(2026, 7, 4),
                ),
            ]

        _patch_gateway(monkeypatch, list_authorizations)

        rows = {row.session_role: row for row in refresh_account_environment_observations(session, tenant_id=1, actor="tester")}
        snapshots = list(session.scalars(select(TgAccountAuthorizationSnapshot).order_by(TgAccountAuthorizationSnapshot.authorization_id)))

    assert rows["primary"].observed_device_model == "iPhone 15"
    assert rows["standby_1"].observed_device_model == "Pixel 8"
    assert [(snapshot.authorization_id, snapshot.developer_app_id, snapshot.session_role) for snapshot in snapshots] == [
        (201, 11, "primary"),
        (202, 11, "standby_1"),
    ]


def _add_environment_binding(
    session: Session,
    *,
    auth_id: int,
    role: str,
    device: str,
) -> None:
    session.add(
        AccountEnvironmentBinding(
            tenant_id=1,
            account_id=101,
            developer_app_id=11,
            developer_app_api_id_snapshot=10011,
            authorization_id=auth_id,
            session_role=role,
            proxy_id=31,
            device_model=device,
            system_version="iOS 17.5" if role == "primary" else "Android 14",
            app_version="10.14.1",
            platform="ios" if role == "primary" else "android",
            client_identity_key=f"client-{auth_id}",
        )
    )
    session.commit()


def _patch_gateway(monkeypatch, list_authorizations) -> None:
    monkeypatch.setattr(observation_service, "credentials_for_developer_app", lambda *_args: SimpleNamespace())
    monkeypatch.setattr(observation_service.gateway, "list_authorizations", list_authorizations)
