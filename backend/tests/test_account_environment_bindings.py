from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy import select
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
from app.schemas.account_environment import AccountEnvironmentBindingPatch
from app.security import encrypt_secret
from app.services.account_environment import (
    list_account_environment_bindings,
    patch_account_environment_binding,
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


def _add_standby_authorization(session: Session) -> None:
    session.add(
        TgAccountAuthorization(
            id=202,
            tenant_id=1,
            account_id=101,
            role="standby_1",
            developer_app_id=11,
            developer_app_api_id_snapshot=10011,
            proxy_id=31,
            session_ciphertext="enc-session-standby",
            status="standby",
        )
    )
    session.commit()


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


def test_account_environment_projection_marks_incomplete_snapshot_unobservable() -> None:
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
                system_version="",
                app_version="10.14.1",
                api_id=10011,
            )
        )
        session.commit()

        row = list_account_environment_bindings(session, tenant_id=1)[0]

    assert row.consistency_status == "unobservable"
    assert row.observed_device_model == "iPhone 15"
    assert row.observed_system_version == ""
    assert row.observed_missing_fields == ["system_version"]


def test_account_environment_projection_matches_snapshot_by_authorization_hash() -> None:
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
                telegram_authorization_hash_ciphertext=encrypt_secret("standby-hash"),
            )
        )
        session.get(TgAccountAuthorization, 201).telegram_authorization_hash_ciphertext = encrypt_secret("primary-hash")
        for auth_id, role, device in [(201, "primary", "iPhone 15"), (202, "standby_1", "Pixel 8")]:
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
        session.add_all(
            [
                TgAccountAuthorizationSnapshot(
                    tenant_id=1,
                    account_id=101,
                    authorization_hash_ciphertext=encrypt_secret("primary-hash"),
                    is_current_session=True,
                    device_model="iPhone 15",
                    system_version="iOS 17.5",
                    app_version="10.14.1",
                    api_id=10011,
                ),
                TgAccountAuthorizationSnapshot(
                    tenant_id=1,
                    account_id=101,
                    authorization_hash_ciphertext=encrypt_secret("standby-hash"),
                    is_current_session=False,
                    device_model="Pixel 8",
                    system_version="Android 14",
                    app_version="10.14.1",
                    api_id=10011,
                ),
            ]
        )
        session.commit()

        rows = {row.session_role: row for row in list_account_environment_bindings(session, tenant_id=1)}

    assert rows["primary"].observed_device_model == "iPhone 15"
    assert rows["primary"].consistency_status == "observed_matched"
    assert rows["standby_1"].observed_device_model == "Pixel 8"
    assert rows["standby_1"].consistency_status == "observed_matched"


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
    proxy_binding = session.query(AccountProxyBinding).one()
    assert proxy_binding.developer_app_id == 11
    assert proxy_binding.developer_app_api_id_snapshot == 10011
    assert proxy_binding.authorization_id == 201
    assert proxy_binding.session_role == "primary"


def test_patch_account_environment_binding_rebinds_proxy_without_active_slot_conflict() -> None:
    with _session() as session:
        _seed_environment(session)
        session.add(AccountProxy(id=32, tenant_id=1, name="节点B", protocol="socks5", host="127.0.0.2", port=1081, status="healthy", alert_status="normal"))
        for proxy_id in (31, 32):
            patch_account_environment_binding(
                session,
                tenant_id=1,
                account_id=101,
                payload=AccountEnvironmentBindingPatch(
                    developer_app_id=11,
                    authorization_id=201,
                    session_role="primary",
                    proxy_id=proxy_id,
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

        binding = session.query(AccountEnvironmentBinding).one()
        proxy_bindings = session.query(AccountProxyBinding).order_by(AccountProxyBinding.id).all()

    assert binding.proxy_id == 32
    assert [row.proxy_id for row in proxy_bindings] == [31, 32]
    assert proxy_bindings[0].status == "inactive"
    assert proxy_bindings[0].unbound_at is not None
    assert proxy_bindings[1].status == "active"
    assert proxy_bindings[1].unbound_at is None


def test_patch_account_environment_binding_rejects_reused_client_identity_key() -> None:
    with _session() as session:
        _seed_environment(session)
        _add_standby_authorization(session)
        patch_account_environment_binding(
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

        with pytest.raises(ValueError, match="client_identity_key_reused"):
            patch_account_environment_binding(
                session,
                tenant_id=1,
                account_id=101,
                payload=AccountEnvironmentBindingPatch(
                    developer_app_id=11,
                    authorization_id=202,
                    session_role="standby_1",
                    proxy_id=31,
                    device_model="Pixel 8",
                    system_version="Android 14",
                    app_version="10.14.1",
                    platform="android",
                    lang_code="zh",
                    system_lang_code="zh-CN",
                    lang_pack="",
                    region_code="CN",
                    client_identity_key="manual-client-1",
                ),
                actor="tester",
            )


def test_patch_account_environment_binding_rejects_reused_device_combo() -> None:
    with _session() as session:
        _seed_environment(session)
        _add_standby_authorization(session)
        patch_account_environment_binding(
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

        with pytest.raises(ValueError, match="client_metadata_combo_reused"):
            patch_account_environment_binding(
                session,
                tenant_id=1,
                account_id=101,
                payload=AccountEnvironmentBindingPatch(
                    developer_app_id=11,
                    authorization_id=202,
                    session_role="standby_1",
                    proxy_id=31,
                    device_model="iPhone 15 Pro",
                    system_version="iOS 17.5",
                    app_version="10.14.1",
                    platform="ios",
                    lang_code="zh",
                    system_lang_code="zh-CN",
                    lang_pack="",
                    region_code="CN",
                    client_identity_key="manual-client-2",
                ),
                actor="tester",
            )


def test_account_environment_proxy_binding_is_authorization_slot_scoped() -> None:
    with _session() as session:
        _seed_environment(session)
        _add_standby_authorization(session)

        for auth_id, role, identity in [(201, "primary", "manual-client-1"), (202, "standby_1", "manual-client-2")]:
            patch_account_environment_binding(
                session,
                tenant_id=1,
                account_id=101,
                payload=AccountEnvironmentBindingPatch(
                    developer_app_id=11,
                    authorization_id=auth_id,
                    session_role=role,
                    proxy_id=31,
                    device_model=f"iPhone {auth_id}",
                    system_version="iOS 17.5",
                    app_version="10.14.1",
                    platform="ios",
                    lang_code="zh",
                    system_lang_code="zh-CN",
                    lang_pack="",
                    region_code="CN",
                    client_identity_key=identity,
                ),
                actor="tester",
            )
        session.commit()

        bindings = session.query(AccountProxyBinding).order_by(AccountProxyBinding.authorization_id).all()

    assert [(row.authorization_id, row.session_role, row.proxy_id) for row in bindings] == [(201, "primary", 31), (202, "standby_1", 31)]
