from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountEnvironmentBinding,
    AccountProxy,
    AccountProxyBinding,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgAccountAuthorizationSnapshot,
)
from app.schemas.account_environment import AccountEnvironmentBindingPatch, ProxyAirportSubscriptionUpdate
from app.services.account_environment import list_account_environment_bindings, patch_account_environment_binding
from app.services.proxy_airport_subscription import (
    get_proxy_airport_subscription,
    mask_subscription_url,
    update_proxy_airport_subscription,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    return session


def _seed_environment(session: Session) -> None:
    app = TelegramDeveloperApp(
        id=11,
        app_name="TG App A",
        api_id=10011,
        api_hash_ciphertext="enc",
        is_active=True,
    )
    account = TgAccount(
        id=101,
        tenant_id=1,
        display_name="账号A",
        username="acct_a",
        phone_masked="***1234",
        status="在线",
    )
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
    proxy = AccountProxy(
        id=31,
        tenant_id=1,
        name="节点A",
        protocol="socks5",
        host="127.0.0.1",
        port=1080,
        status="healthy",
        alert_status="normal",
    )
    session.add_all([app, account, authorization, proxy])
    session.commit()


def test_proxy_airport_subscription_masks_url_and_encrypts_raw_value() -> None:
    with _session() as session:
        saved = update_proxy_airport_subscription(
            session,
            tenant_id=1,
            payload=ProxyAirportSubscriptionUpdate(subscription_url="https://example.com/sub?token=secret-token"),
            actor="tester",
        )
        session.commit()

        loaded = get_proxy_airport_subscription(session, tenant_id=1)

    assert saved.subscription_url_configured is True
    assert saved.subscription_url_preview == "https://example.com/sub?...oken"
    assert loaded.subscription_url_preview == "https://example.com/sub?...oken"
    assert "secret-token" not in loaded.model_dump_json()


def test_mask_subscription_url_never_returns_full_token() -> None:
    assert mask_subscription_url("https://xsus.example/sub/path?token=878be1154cad71f3208c1c66d7af82ca") == "https://xsus.example/sub/path?...82ca"


def test_account_environment_projection_uses_developer_app_and_authorization_slot() -> None:
    with _session() as session:
        _seed_environment(session)
        binding = AccountEnvironmentBinding(
            tenant_id=1,
            account_id=101,
            developer_app_id=11,
            developer_app_api_id_snapshot=10011,
            authorization_id=201,
            session_role="primary",
            proxy_id=31,
            device_model="iPhone 15",
            system_version="iOS 17.5",
            app_version="10.14.1",
            platform="ios",
            client_identity_key="client-1",
        )
        session.add(binding)
        session.commit()

        rows = list_account_environment_bindings(session, tenant_id=1)

    assert rows[0].account_id == 101
    assert rows[0].developer_app_id == 11
    assert rows[0].developer_app_api_id_snapshot == 10011
    assert rows[0].authorization_id == 201
    assert rows[0].consistency_status == "pending_effect"


def test_account_environment_projection_compares_remote_authorization_snapshot() -> None:
    with _session() as session:
        _seed_environment(session)
        session.add(
            AccountEnvironmentBinding(
                tenant_id=1,
                account_id=101,
                developer_app_id=11,
                developer_app_api_id_snapshot=10011,
                authorization_id=201,
                session_role="primary",
                proxy_id=31,
                device_model="iPhone 15",
                system_version="iOS 17.5",
                app_version="10.14.1",
                platform="ios",
                client_identity_key="client-1",
            )
        )
        session.add(
            TgAccountAuthorizationSnapshot(
                tenant_id=1,
                account_id=101,
                is_current_session=True,
                device_model="iPhone 15",
                system_version="iOS 17.5",
                app_version="10.14.1",
                api_id=10011,
            )
        )
        session.commit()

        matched = list_account_environment_bindings(session, tenant_id=1)[0]
        session.query(TgAccountAuthorizationSnapshot).update({"device_model": "iPhone 12"})
        session.commit()
        mismatch = list_account_environment_bindings(session, tenant_id=1)[0]

    assert matched.observed_device_model == "iPhone 15"
    assert matched.observed_api_id == 10011
    assert matched.consistency_status == "observed_matched"
    assert mismatch.observed_device_model == "iPhone 12"
    assert mismatch.consistency_status == "observed_mismatch"


def test_patch_account_environment_binding_persists_app_scoped_fingerprint() -> None:
    with _session() as session:
        _seed_environment(session)
        row = patch_account_environment_binding(
            session,
            tenant_id=1,
            account_id=101,
            payload=AccountEnvironmentBindingPatch(
                developer_app_id=11,
                authorization_id=201,
                session_role="primary",
                proxy_id=31,
                device_model="iPhone 15 Pro",
                system_version="iOS 17.5",
                app_version="10.14.1",
                platform="ios",
                lang_code="zh",
                system_lang_code="zh-CN",
                lang_pack="",
                region_code="CN",
                client_identity_key="manual-client-1",
            ),
            actor="tester",
        )
        session.commit()

        saved = session.query(AccountEnvironmentBinding).one()

    assert row.developer_app_id == 11
    assert row.developer_app_api_id_snapshot == 10011
    assert row.consistency_status == "pending_effect"
    assert saved.developer_app_id == 11
    assert saved.client_identity_key == "manual-client-1"
    assert saved.proxy_binding_id is not None
    assert session.query(AccountProxyBinding).count() == 1
