from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routers.account_pools import router as account_pools_router
from app.auth import create_admin_access_token
from app.database import Base, get_session
from app.models import (
    AccountGroupProxyBinding,
    AccountPool,
    AccountProxy,
    ProxyAirportNode,
    ProxyAirportSubscription,
    Task,
    Tenant,
)
from app.permission_middleware import permission_middleware
from app.services import proxy_group_binding_service


pytestmark = pytest.mark.no_postgres


def _engine():
    return create_engine("sqlite:///:memory:", future=True)


def _seed(session: Session) -> None:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(ProxyAirportSubscription(id=1, tenant_id=1, name="主订阅", enabled=True, sync_status="synced", healthy_node_count=4))
    session.add(AccountPool(id=10, tenant_id=1, name="降权分组A", pool_purpose="rank_deboost"))
    session.add(AccountPool(id=11, tenant_id=1, name="降权分组B", pool_purpose="rank_deboost"))
    session.add(ProxyAirportNode(id=20, tenant_id=1, subscription_id=1, node_key="socks", protocol="socks5", proxy_host="10.0.0.20", proxy_port=1080, status="healthy", observed_exit_ip="1.1.1.1"))
    session.add(ProxyAirportNode(id=21, tenant_id=1, subscription_id=1, node_key="http", protocol="http", proxy_host="10.0.0.21", proxy_port=8080, status="healthy", observed_exit_ip="2.2.2.2"))
    session.add(ProxyAirportNode(id=22, tenant_id=1, subscription_id=1, node_key="vmess", protocol="vmess", proxy_host="vmess.example", proxy_port=443, status="healthy"))
    session.add(ProxyAirportNode(id=23, tenant_id=1, subscription_id=1, node_key="missing-protocol", protocol="", proxy_host="10.0.0.23", proxy_port=1083, status="healthy"))
    session.commit()


@pytest.fixture
def session() -> Session:
    engine = _engine()
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed(db)
        yield db


def test_create_or_update_rank_binding_materializes_socks_runtime_proxy(session: Session) -> None:
    binding = proxy_group_binding_service.create_or_update_rank_deboost_proxy_binding(
        session,
        tenant_id=1,
        account_pool_id=10,
        proxy_airport_node_id=20,
        operator="alice",
        reason="initial",
    )

    runtime_proxy = session.get(AccountProxy, binding.runtime_proxy_id)
    assert runtime_proxy is not None
    assert runtime_proxy.protocol == "socks5"
    assert runtime_proxy.host == "10.0.0.20"
    assert runtime_proxy.port == 1080
    assert binding.proxy_airport_node_id == 20
    assert binding.binding_generation == 1


def test_same_node_binding_is_idempotent(session: Session) -> None:
    first = proxy_group_binding_service.create_or_update_rank_deboost_proxy_binding(
        session,
        tenant_id=1,
        account_pool_id=10,
        proxy_airport_node_id=20,
        operator="alice",
        reason="initial",
    )
    second = proxy_group_binding_service.create_or_update_rank_deboost_proxy_binding(
        session,
        tenant_id=1,
        account_pool_id=10,
        proxy_airport_node_id=20,
        operator="alice",
        reason="repeat",
    )

    assert second.id == first.id
    assert second.runtime_proxy_id == first.runtime_proxy_id
    assert second.binding_generation == 1
    assert session.scalar(select(AccountGroupProxyBinding).where(AccountGroupProxyBinding.status == "active")).id == first.id


def test_switch_binding_increments_generation_and_unbinds_old(session: Session) -> None:
    old = proxy_group_binding_service.create_or_update_rank_deboost_proxy_binding(
        session,
        tenant_id=1,
        account_pool_id=10,
        proxy_airport_node_id=20,
        operator="alice",
        reason="initial",
    )
    new = proxy_group_binding_service.create_or_update_rank_deboost_proxy_binding(
        session,
        tenant_id=1,
        account_pool_id=10,
        proxy_airport_node_id=21,
        operator="alice",
        reason="switch_node",
    )

    session.refresh(old)
    assert old.status == "unbound"
    assert old.unbound_at is not None
    assert old.change_reason == "switch_node"
    assert new.id != old.id
    assert new.proxy_airport_node_id == 21
    assert new.binding_generation == 2
    assert new.status == "active"


def test_raw_vmess_node_without_runtime_proxy_rejected(session: Session) -> None:
    with pytest.raises(ValueError, match="executable runtime proxy"):
        proxy_group_binding_service.create_or_update_rank_deboost_proxy_binding(
            session,
            tenant_id=1,
            account_pool_id=10,
            proxy_airport_node_id=22,
            operator="alice",
            reason="raw_vmess",
        )

    assert session.scalar(select(AccountProxy).where(AccountProxy.name == "airport-node-22")) is None
    assert session.scalar(select(AccountGroupProxyBinding)) is None


def test_airport_node_with_blank_protocol_is_rejected(session: Session) -> None:
    with pytest.raises(ValueError, match="executable runtime proxy"):
        proxy_group_binding_service.create_or_update_rank_deboost_proxy_binding(
            session,
            tenant_id=1,
            account_pool_id=10,
            proxy_airport_node_id=23,
            operator="alice",
            reason="missing_protocol",
        )

    assert session.scalar(select(AccountProxy).where(AccountProxy.name == "airport-node-23")) is None
    assert session.scalar(select(AccountGroupProxyBinding)) is None


def test_stale_materialized_proxy_is_synced_to_current_node_endpoint(session: Session) -> None:
    session.add(
        AccountProxy(
            tenant_id=1,
            name="airport-node-20",
            protocol="socks5",
            host="old.example",
            port=9999,
            status="healthy",
        )
    )
    session.commit()

    binding = proxy_group_binding_service.create_or_update_rank_deboost_proxy_binding(
        session,
        tenant_id=1,
        account_pool_id=10,
        proxy_airport_node_id=20,
        operator="alice",
        reason="sync_stale_proxy",
    )

    runtime_proxy = session.get(AccountProxy, binding.runtime_proxy_id)
    assert runtime_proxy is not None
    assert (runtime_proxy.protocol, runtime_proxy.host, runtime_proxy.port) == ("socks5", "10.0.0.20", 1080)


def test_delete_binding_rejects_running_rank_task_reference(session: Session) -> None:
    proxy_group_binding_service.create_or_update_rank_deboost_proxy_binding(
        session,
        tenant_id=1,
        account_pool_id=10,
        proxy_airport_node_id=20,
        operator="alice",
        reason="initial",
    )
    session.add(Task(tenant_id=1, name="观察任务", type="search_rank_deboost", status="running", type_config={"account_pool_id": 10}))
    session.commit()

    with pytest.raises(ValueError, match="running/paused"):
        proxy_group_binding_service.delete_rank_deboost_proxy_binding(
            session,
            tenant_id=1,
            account_pool_id=10,
            operator="alice",
            reason="manual",
        )


def test_delete_binding_unbinds_when_no_reference(session: Session) -> None:
    binding = proxy_group_binding_service.create_or_update_rank_deboost_proxy_binding(
        session,
        tenant_id=1,
        account_pool_id=10,
        proxy_airport_node_id=20,
        operator="alice",
        reason="initial",
    )

    deleted = proxy_group_binding_service.delete_rank_deboost_proxy_binding(
        session,
        tenant_id=1,
        account_pool_id=10,
        operator="alice",
        reason="manual_unbind",
    )

    assert deleted.id == binding.id
    assert deleted.status == "unbound"
    assert deleted.unbound_at is not None
    assert deleted.change_reason == "manual_unbind"


def test_rank_binding_api_put_and_delete_smoke() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as db:
        _seed(db)

    def override_session():
        with session_factory() as db:
            yield db

    app = FastAPI()
    app.middleware("http")(permission_middleware)
    app.include_router(account_pools_router)
    app.dependency_overrides[get_session] = override_session
    headers = {"Authorization": f"Bearer {create_admin_access_token()}"}

    with TestClient(app, raise_server_exceptions=False) as client:
        put_response = client.put(
            "/api/account-pools/10/rank-deboost-proxy-binding",
            headers=headers,
            json={"proxy_airport_node_id": 20, "reason": "api"},
        )
        assert put_response.status_code == 200
        put_payload = put_response.json()
        assert put_payload["runtime_proxy_id"] is not None
        assert put_payload["reference_count"] == 0
        assert {"host", "port", "username", "password", "password_ciphertext"}.isdisjoint(put_payload)
        assert set(put_payload) == {
            "id",
            "tenant_id",
            "account_pool_id",
            "proxy_airport_node_id",
            "runtime_proxy_id",
            "binding_generation",
            "status",
            "observed_exit_ip",
            "observed_exit_country",
            "observed_exit_asn",
            "observed_exit_isp",
            "last_probe_at",
            "last_probe_error",
            "reference_count",
        }
        delete_response = client.request(
            "DELETE",
            "/api/account-pools/10/rank-deboost-proxy-binding",
            headers=headers,
            json={"reason": "api_unbind"},
        )
        assert delete_response.status_code == 200
        assert delete_response.json()["status"] == "unbound"
