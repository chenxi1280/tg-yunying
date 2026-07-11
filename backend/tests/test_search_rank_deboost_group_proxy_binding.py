from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    AccountProxy,
    AccountProxyBinding,
    ProxyAirportNode,
    ProxyAirportSubscription,
    Tenant,
)
from app.services.proxy_group_binding_service import (
    create_group_proxy_binding,
    failover_group_proxy_binding,
    get_active_group_binding,
    unbind_group_proxy_binding,
    verify_group_proxy_egress,
)


pytestmark = pytest.mark.no_postgres


def _build_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed_base(session: Session) -> None:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(ProxyAirportSubscription(id=1, tenant_id=1, name="主订阅", enabled=True, sync_status="synced", healthy_node_count=3))
    session.add(AccountPool(id=10, tenant_id=1, name="降权分组A", pool_purpose="rank_deboost"))
    session.add(AccountPool(id=12, tenant_id=1, name="降权分组B", pool_purpose="rank_deboost"))
    session.add(AccountPool(id=11, tenant_id=1, name="普通分组", pool_purpose="normal"))
    session.add(ProxyAirportNode(id=20, tenant_id=1, subscription_id=1, node_key="node-20", protocol="socks5", proxy_host="127.0.0.20", proxy_port=1080, status="healthy", observed_exit_ip="1.1.1.1"))
    session.add(ProxyAirportNode(id=21, tenant_id=1, subscription_id=1, node_key="node-21", protocol="socks5", proxy_host="127.0.0.21", proxy_port=1081, status="healthy", observed_exit_ip="2.2.2.2"))
    session.add(ProxyAirportNode(id=23, tenant_id=1, subscription_id=1, node_key="node-23", status="unhealthy"))
    session.commit()


# --- SubTask 16.1 / 16.2: create_group_proxy_binding ---


def test_create_group_binding_succeeds() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        binding = create_group_proxy_binding(
            session,
            tenant_id=1,
            account_pool_id=10,
            proxy_airport_node_id=20,
            operator="alice",
        )
        assert binding.id is not None
        assert binding.status == "active"
        assert binding.binding_scope == "group"
        assert binding.account_pool_id == 10
        assert binding.proxy_airport_node_id == 20
        assert binding.binding_generation == 1
        assert binding.runtime_proxy_id is not None
        assert binding.observed_exit_ip == "1.1.1.1"
        assert binding.bound_by == "alice"
        assert binding.unbound_at is None
        runtime_proxy = session.get(AccountProxy, binding.runtime_proxy_id)
        assert runtime_proxy is not None
        assert runtime_proxy.protocol == "socks5"
        assert runtime_proxy.host == "127.0.0.20"
        assert runtime_proxy.port == 1080

        active = get_active_group_binding(session, tenant_id=1, account_pool_id=10)
        assert active is not None
        assert active.id == binding.id


def test_create_group_binding_rejects_non_rank_deboost_pool() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        with pytest.raises(ValueError, match="rank_deboost"):
            create_group_proxy_binding(
                session,
                tenant_id=1,
                account_pool_id=11,
                proxy_airport_node_id=20,
                operator="alice",
            )


def test_create_group_binding_rejects_unhealthy_node() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        with pytest.raises(ValueError, match="不可用"):
            create_group_proxy_binding(
                session,
                tenant_id=1,
                account_pool_id=10,
                proxy_airport_node_id=23,
                operator="alice",
            )


def test_create_group_binding_rejects_node_used_by_authorization_slot() -> None:
    """spec Scenario: 分组级绑定独占节点。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        session.add(
            AccountProxyBinding(
                tenant_id=1,
                account_id=999,
                proxy_airport_node_id=20,
                status="active",
            )
        )
        session.commit()

        with pytest.raises(ValueError, match="授权槽位级绑定占用"):
            create_group_proxy_binding(
                session,
                tenant_id=1,
                account_pool_id=10,
                proxy_airport_node_id=20,
                operator="alice",
            )


def test_create_group_binding_rejects_node_used_by_other_group() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        # 先在分组 12 上绑定节点 20
        create_group_proxy_binding(
            session,
            tenant_id=1,
            account_pool_id=12,
            proxy_airport_node_id=20,
            operator="bob",
        )
        # 再尝试在分组 10 上绑定同一节点 20
        with pytest.raises(ValueError, match="已被其他降权分组绑定"):
            create_group_proxy_binding(
                session,
                tenant_id=1,
                account_pool_id=10,
                proxy_airport_node_id=20,
                operator="alice",
            )


# --- get_active_group_binding ---


def test_get_active_group_binding_returns_active_only() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        assert get_active_group_binding(session, tenant_id=1, account_pool_id=10) is None

        binding = create_group_proxy_binding(
            session,
            tenant_id=1,
            account_pool_id=10,
            proxy_airport_node_id=20,
            operator="alice",
        )
        assert get_active_group_binding(session, tenant_id=1, account_pool_id=10) is not None

        unbind_group_proxy_binding(session, binding_id=binding.id, reason="test", operator="alice")
        assert get_active_group_binding(session, tenant_id=1, account_pool_id=10) is None


# --- unbind_group_proxy_binding ---


def test_unbind_group_binding_sets_unbound_at() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        binding = create_group_proxy_binding(
            session,
            tenant_id=1,
            account_pool_id=10,
            proxy_airport_node_id=20,
            operator="alice",
        )
        assert binding.unbound_at is None
        assert binding.status == "active"

        unbind_group_proxy_binding(
            session, binding_id=binding.id, reason="manual_unbind", operator="alice"
        )
        session.refresh(binding)
        assert binding.status == "unbound"
        assert binding.unbound_at is not None
        assert binding.change_reason == "manual_unbind"


# --- failover_group_proxy_binding ---


def test_failover_creates_new_binding_and_increments_generation() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        old_binding = create_group_proxy_binding(
            session,
            tenant_id=1,
            account_pool_id=10,
            proxy_airport_node_id=20,
            operator="alice",
        )
        assert old_binding.binding_generation == 1
        assert old_binding.proxy_airport_node_id == 20

        new_binding = failover_group_proxy_binding(
            session,
            tenant_id=1,
            account_pool_id=10,
            reason="node_degraded",
            operator="alice",
        )

        # 旧绑定已 unbind
        session.refresh(old_binding)
        assert old_binding.status == "unbound"
        assert old_binding.unbound_at is not None
        assert old_binding.last_failover_at is not None

        # 新绑定 active 且 generation + 1
        assert new_binding.status == "active"
        assert new_binding.binding_generation == 2
        assert new_binding.proxy_airport_node_id == 21
        assert new_binding.runtime_proxy_id is not None
        runtime_proxy = session.get(AccountProxy, new_binding.runtime_proxy_id)
        assert runtime_proxy is not None
        assert runtime_proxy.host == "127.0.0.21"
        assert runtime_proxy.port == 1081
        assert new_binding.account_pool_id == 10
        assert new_binding.last_failover_at is not None

        # get_active_group_binding 返回新绑定
        active = get_active_group_binding(session, tenant_id=1, account_pool_id=10)
        assert active is not None
        assert active.id == new_binding.id


# --- verify_group_proxy_egress ---


def test_verify_group_proxy_egress_detects_ip_drift() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        binding = create_group_proxy_binding(
            session,
            tenant_id=1,
            account_pool_id=10,
            proxy_airport_node_id=20,
            operator="alice",
        )
        # 节点 20 observed_exit_ip="1.1.1.1"，绑定创建时已复制
        assert binding.observed_exit_ip == "1.1.1.1"
        assert binding.last_health_check_at is None

        # IP 漂移 → False
        assert verify_group_proxy_egress(session, binding_id=binding.id, probe_exit_ip="9.9.9.9") is False

        # 探测失败（probe_exit_ip=None）→ False
        assert verify_group_proxy_egress(session, binding_id=binding.id, probe_exit_ip=None) is False

        # 探测 IP 一致 → True，且 last_health_check_at 已更新
        assert verify_group_proxy_egress(session, binding_id=binding.id, probe_exit_ip="1.1.1.1") is True
        session.refresh(binding)
        assert binding.last_health_check_at is not None
        assert binding.last_probe_at is not None
        assert binding.last_probe_error == ""
        assert binding.observed_exit_ip == "1.1.1.1"
