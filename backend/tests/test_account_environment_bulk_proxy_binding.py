from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountEnvironmentBinding,
    AccountPool,
    AccountProxy,
    AccountProxyBinding,
    ProxyAirportNode,
    ProxyAirportSubscription,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
)
from app.schemas.account_environment import AccountEnvironmentProxyBatchBindRequest
from app.services.account_environment_bulk import bind_account_environment_proxy_batch


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    return session


def _seed_pool_environment(session: Session) -> None:
    session.add(AccountPool(id=7, tenant_id=1, name="搜索账号组", description="", is_default=False))
    session.add(TelegramDeveloperApp(id=11, app_name="TG App A", api_id=10011, api_hash_ciphertext="enc", is_active=True))
    session.add_all(
        [
            TgAccount(id=101, tenant_id=1, pool_id=7, display_name="账号A", username="acct_a", phone_masked="***101", status="在线"),
            TgAccount(id=102, tenant_id=1, pool_id=7, display_name="账号B", username="acct_b", phone_masked="***102", status="在线"),
        ]
    )
    session.add(
        TgAccountAuthorization(
            id=201,
            tenant_id=1,
            account_id=101,
            role="primary",
            developer_app_id=11,
            developer_app_api_id_snapshot=10011,
            session_ciphertext="enc-session",
            status="active",
            is_current=True,
        )
    )
    session.add_all(
        [
            AccountProxy(id=31, tenant_id=1, name="旧节点", protocol="socks5", host="127.0.0.1", port=1080, status="healthy", alert_status="normal"),
            AccountProxy(id=32, tenant_id=1, name="新节点", protocol="socks5", host="127.0.0.2", port=1081, status="healthy", alert_status="normal"),
        ]
    )
    session.add(
        AccountEnvironmentBinding(
            id="env-101",
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
            client_identity_key="client-101",
        )
    )
    session.commit()


def test_bind_account_environment_proxy_batch_uses_account_pool_without_creating_missing_environment() -> None:
    with _session() as session:
        _seed_pool_environment(session)

        result = bind_account_environment_proxy_batch(
            session,
            tenant_id=1,
            payload=AccountEnvironmentProxyBatchBindRequest(account_pool_id=7, proxy_id=32, session_role="primary", change_reason="按分组切换代理"),
            actor="tester",
        )
        binding = session.get(AccountEnvironmentBinding, "env-101")
        proxy_bindings = list(session.scalars(select(AccountProxyBinding).order_by(AccountProxyBinding.id)))

    assert result.success_count == 1
    assert result.failed_count == 1
    assert result.affected_account_ids == [101]
    assert result.skipped_accounts == [{"account_id": 102, "reason": "account_environment_binding_missing"}]
    assert binding.proxy_id == 32
    assert binding.proxy_binding_id == proxy_bindings[0].id
    assert [(row.account_id, row.authorization_id, row.session_role, row.proxy_id, row.status) for row in proxy_bindings] == [
        (101, 201, "primary", 32, "active")
    ]
    assert session.scalar(select(AccountEnvironmentBinding).where(AccountEnvironmentBinding.account_id == 102)) is None


def test_bind_account_environment_proxy_batch_updates_all_active_authorization_slots() -> None:
    with _session() as session:
        _seed_pool_environment(session)
        session.add(
            AccountEnvironmentBinding(
                id="env-101-second-app",
                tenant_id=1,
                account_id=101,
                developer_app_id=11,
                developer_app_api_id_snapshot=10011,
                authorization_id=202,
                session_role="primary",
                proxy_id=31,
                device_model="iPhone 15 Pro",
                system_version="iOS 17.5",
                app_version="10.14.1",
                platform="ios",
                client_identity_key="client-101-second-app",
            )
        )
        session.commit()

        result = bind_account_environment_proxy_batch(
            session,
            tenant_id=1,
            payload=AccountEnvironmentProxyBatchBindRequest(account_pool_id=7, proxy_id=32, session_role="primary", change_reason="按分组切换代理"),
            actor="tester",
        )
        environment_proxy_ids = list(
            session.scalars(
                select(AccountEnvironmentBinding.proxy_id)
                .where(AccountEnvironmentBinding.account_id == 101)
                .order_by(AccountEnvironmentBinding.authorization_id)
            )
        )
        proxy_bindings = list(session.scalars(select(AccountProxyBinding).order_by(AccountProxyBinding.authorization_id)))

    assert result.success_count == 1
    assert result.affected_account_ids == [101]
    assert environment_proxy_ids == [32, 32]
    assert [(row.authorization_id, row.proxy_id, row.status) for row in proxy_bindings] == [
        (201, 32, "active"),
        (202, 32, "active"),
    ]


def test_bind_account_environment_proxy_batch_accepts_healthy_clash_node() -> None:
    with _session() as session:
        _seed_pool_environment(session)
        session.add(
            ProxyAirportSubscription(
                id=41,
                tenant_id=1,
                name="香港 Clash",
                enabled=True,
                sync_status="synced",
                node_count=1,
                healthy_node_count=1,
            )
        )
        session.add(
            ProxyAirportNode(
                id=51,
                tenant_id=1,
                subscription_id=41,
                node_key="hk-01",
                node_name="HK 01",
                protocol="socks5",
                proxy_host="10.0.0.51",
                proxy_port=9051,
                status="healthy",
                observed_exit_ip="8.8.8.8",
                observed_exit_country="HK",
            )
        )
        session.commit()

        result = bind_account_environment_proxy_batch(
            session,
            tenant_id=1,
            payload=AccountEnvironmentProxyBatchBindRequest(account_pool_id=7, proxy_airport_node_id=51, session_role="primary", change_reason="按分组切换 Clash 节点"),
            actor="tester",
        )
        binding = session.get(AccountEnvironmentBinding, "env-101")
        proxy_binding = session.scalar(select(AccountProxyBinding).where(AccountProxyBinding.status == "active"))
        proxy = session.get(AccountProxy, binding.proxy_id)

    assert result.success_count == 1
    assert result.failed_count == 1
    assert binding.proxy_binding_id == proxy_binding.id
    assert proxy.host == "10.0.0.51"
    assert proxy.port == 9051
    assert proxy.protocol == "socks5"
    assert proxy_binding.proxy_airport_node_id == 51
    assert proxy_binding.observed_exit_ip == "8.8.8.8"
    assert proxy_binding.observed_exit_country == "HK"


def test_bind_account_environment_proxy_batch_rejects_unhealthy_clash_node() -> None:
    with _session() as session:
        _seed_pool_environment(session)
        session.add(ProxyAirportSubscription(id=41, tenant_id=1, name="香港 Clash", enabled=True, sync_status="synced"))
        session.add(
            ProxyAirportNode(
                id=52,
                tenant_id=1,
                subscription_id=41,
                node_key="hk-02",
                node_name="HK 02",
                protocol="socks5",
                proxy_host="10.0.0.52",
                proxy_port=9052,
                status="unhealthy",
            )
        )
        session.commit()

        with pytest.raises(ValueError, match="proxy_airport_node_not_available"):
            bind_account_environment_proxy_batch(
                session,
                tenant_id=1,
                payload=AccountEnvironmentProxyBatchBindRequest(account_pool_id=7, proxy_airport_node_id=52, session_role="primary", change_reason="不可绑定异常节点"),
                actor="tester",
            )


def test_bind_account_environment_proxy_batch_rejects_code_receiver_pool() -> None:
    with _session() as session:
        _seed_pool_environment(session)
        pool = session.get(AccountPool, 7)
        pool.pool_purpose = "code_receiver"
        pool.system_key = "code_receiver"
        session.commit()

        with pytest.raises(ValueError, match="account_pool_not_operational"):
            bind_account_environment_proxy_batch(
                session,
                tenant_id=1,
                payload=AccountEnvironmentProxyBatchBindRequest(account_pool_id=7, proxy_id=32, session_role="primary", change_reason="禁止接码组"),
                actor="tester",
            )
