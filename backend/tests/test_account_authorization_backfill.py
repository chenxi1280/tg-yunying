from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import pytest

from app.database import Base
from app.integrations.telegram.contracts import AccountAuthorizationSnapshot
from app.models import AccountStatus, TelegramDeveloperApp, Tenant, TgAccount, TgAccountAuthorization
from app.security import decrypt_secret
from app.services import account_authorization_metadata
from app.services.account_authorization_backfill import backfill_standby_authorization_metadata

pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _current_authorization(api_id: int, authorization_hash: str) -> AccountAuthorizationSnapshot:
    return AccountAuthorizationSnapshot(
        authorization_hash=authorization_hash,
        is_current=True,
        device_model="PC 64bit",
        platform="Android",
        system_version="",
        api_id=api_id,
        app_name="standby-app",
        app_version="1.0",
        date_created=datetime(2026, 6, 15),
        date_active=datetime(2026, 7, 2),
    )


def _peer_authorization(api_id: int, authorization_hash: str) -> AccountAuthorizationSnapshot:
    return AccountAuthorizationSnapshot(
        authorization_hash=authorization_hash,
        is_current=False,
        device_model="PC 64bit",
        platform="Android",
        system_version="",
        api_id=api_id,
        app_name="standby-app",
        app_version="1.0",
        date_created=datetime(2026, 6, 15),
        date_active=datetime(2026, 7, 2),
    )


def test_backfills_missing_standby_hash_and_api_snapshot(monkeypatch) -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TelegramDeveloperApp(id=2, app_name="Standby", api_id=222, api_hash_ciphertext="secret"))
        account = TgAccount(id=3, tenant_id=1, display_name="账号", phone_masked="3", status=AccountStatus.ACTIVE.value)
        session.add(account)
        session.flush()
        asset = TgAccountAuthorization(
            tenant_id=1,
            account_id=account.id,
            role="standby_1",
            developer_app_id=2,
            session_ciphertext="standby-session",
            status="standby",
            health_status="healthy",
        )
        session.add(asset)
        session.commit()
        monkeypatch.setattr(account_authorization_metadata, "credentials_for_developer_app", lambda *_args: SimpleNamespace())
        monkeypatch.setattr(
            account_authorization_metadata.gateway,
            "list_authorizations",
            lambda *_args: [_current_authorization(222, "current-hash")],
        )

        result = backfill_standby_authorization_metadata(session, tenant_id=1, apply=True, actor="tester")

        session.refresh(asset)
        assert result["updated_count"] == 1
        assert result["failed_count"] == 0
        assert asset.developer_app_api_id_snapshot == 222
        assert decrypt_secret(asset.telegram_authorization_hash_ciphertext) == "current-hash"


def test_backfills_standby_hash_from_primary_view_when_current_hash_is_zero(monkeypatch) -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TelegramDeveloperApp(id=1, app_name="Primary", api_id=111, api_hash_ciphertext="secret"),
                TelegramDeveloperApp(id=2, app_name="Standby", api_id=222, api_hash_ciphertext="secret"),
            ]
        )
        account = TgAccount(
            id=3,
            tenant_id=1,
            display_name="账号",
            phone_masked="3",
            developer_app_id=1,
            session_ciphertext="primary-session",
            status=AccountStatus.ACTIVE.value,
        )
        session.add(account)
        session.flush()
        asset = TgAccountAuthorization(
            tenant_id=1,
            account_id=account.id,
            role="standby_1",
            developer_app_id=2,
            session_ciphertext="standby-session",
            status="standby",
            health_status="healthy",
        )
        session.add(asset)
        session.commit()

        def list_authorizations(session_ciphertext, *_args):
            if session_ciphertext == "standby-session":
                return [_current_authorization(222, "0")]
            return [_peer_authorization(222, "real-standby-hash")]

        monkeypatch.setattr(account_authorization_metadata, "credentials_for_developer_app", lambda *_args: SimpleNamespace())
        monkeypatch.setattr(account_authorization_metadata, "credentials_for_account", lambda *_args: SimpleNamespace())
        monkeypatch.setattr(account_authorization_metadata.gateway, "list_authorizations", list_authorizations)

        result = backfill_standby_authorization_metadata(session, tenant_id=1, apply=True, actor="tester")

        session.refresh(asset)
        assert result["updated_count"] == 1
        assert result["failed_count"] == 0
        assert asset.developer_app_api_id_snapshot == 222
        assert decrypt_secret(asset.telegram_authorization_hash_ciphertext) == "real-standby-hash"


def test_backfill_marks_failed_standby_as_needs_repair(monkeypatch) -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TelegramDeveloperApp(id=2, app_name="Standby", api_id=222, api_hash_ciphertext="secret"))
        session.add(TgAccount(id=3, tenant_id=1, display_name="账号", phone_masked="3", status=AccountStatus.ACTIVE.value))
        session.flush()
        asset = TgAccountAuthorization(
            tenant_id=1,
            account_id=3,
            role="standby_1",
            developer_app_id=2,
            session_ciphertext="standby-session",
            status="standby",
            health_status="healthy",
        )
        session.add(asset)
        session.commit()
        monkeypatch.setattr(account_authorization_metadata, "credentials_for_developer_app", lambda *_args: SimpleNamespace())
        monkeypatch.setattr(
            account_authorization_metadata.gateway,
            "list_authorizations",
            lambda *_args: [_current_authorization(222, "")],
        )

        result = backfill_standby_authorization_metadata(session, tenant_id=1, apply=True, actor="tester")

        session.refresh(asset)
        assert result["updated_count"] == 0
        assert result["failed_count"] == 1
        assert result["failures"][0]["error"] == "current authorization hash missing"
        assert asset.status == "needs_repair"
        assert asset.health_status == "failed"
        assert asset.derived_status == "manual_required"
        assert asset.failure_reason == "备用授权元数据回填失败：current authorization hash missing"
        assert asset.telegram_authorization_hash_ciphertext in ("", None)
