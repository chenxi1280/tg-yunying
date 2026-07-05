from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountProxyBinding,
    AccountProxyWarmupState,
    ProxyAirportNode,
    ProxyAirportSubscription,
    ProxyExitIpObservation,
    ProxyNodeFailoverEvent,
    Tenant,
)
from app.services.proxy_airport_subscription import failover_proxy_airport_node_binding


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    return session


def test_proxy_airport_failover_switches_same_subscription_node_and_resets_warmup() -> None:
    with _session() as session:
        primary = _add_subscription(session, name="primary", priority=10)
        old_node = _add_node(session, primary, "old-node", "unhealthy", "203.0.113.10")
        next_node = _add_node(session, primary, "next-node", "healthy", "203.0.113.11")
        old_binding = _add_binding(session, old_node)
        session.commit()

        new_binding = failover_proxy_airport_node_binding(
            session,
            tenant_id=1,
            proxy_binding_id=old_binding.id,
            reason="proxy_node_unreachable",
            observed_error="connect_timeout",
        )
        session.commit()
        event = session.query(ProxyNodeFailoverEvent).one()
        warmup = session.query(AccountProxyWarmupState).one()
        observation = session.query(ProxyExitIpObservation).one()
        values = _same_subscription_values(old_binding, new_binding, event, warmup, observation)
        node_ids = (old_node.id, next_node.id)

    assert values["old_status"] == "inactive"
    assert values["old_unbound_at"] is not None
    assert values["new_binding_id"] != values["old_binding_id"]
    assert values["new_node_id"] == node_ids[1]
    assert values["new_generation"] == 2
    assert values["new_status"] == "active"
    assert values["event"] == (node_ids[0], node_ids[1], "proxy_node_unreachable", "switched", "connect_timeout")
    assert values["warmup"] == (values["new_binding_id"], "pending_warmup", "proxy_node_unreachable")
    assert values["observation"] == (node_ids[1], values["new_binding_id"], "203.0.113.11")


def test_proxy_airport_failover_uses_backup_subscription_when_primary_has_no_healthy_nodes() -> None:
    with _session() as session:
        primary = _add_subscription(session, name="primary", priority=10)
        backup = _add_subscription(session, name="backup", priority=20)
        old_node = _add_node(session, primary, "old-node", "unhealthy", "203.0.113.10")
        backup_node = _add_node(session, backup, "backup-node", "healthy", "198.51.100.20")
        old_binding = _add_binding(session, old_node)
        session.commit()

        new_binding = failover_proxy_airport_node_binding(
            session,
            tenant_id=1,
            proxy_binding_id=old_binding.id,
            reason="node_removed_from_subscription",
        )
        event = session.query(ProxyNodeFailoverEvent).one()
        event_values = (event.from_subscription_id, event.to_subscription_id)
        subscription_ids = (primary.id, backup.id)

    assert new_binding.proxy_airport_node_id == backup_node.id
    assert event_values == subscription_ids


def test_proxy_airport_failover_accepts_healthy_backup_before_exit_ip_observation() -> None:
    with _session() as session:
        primary = _add_subscription(session, name="primary", priority=10)
        backup = _add_subscription(session, name="backup", priority=20)
        old_node = _add_node(session, primary, "old-node", "unhealthy", "203.0.113.10")
        backup_node = _add_node(session, backup, "backup-node", "healthy", "")
        old_binding = _add_binding(session, old_node)
        session.commit()

        new_binding = failover_proxy_airport_node_binding(
            session,
            tenant_id=1,
            proxy_binding_id=old_binding.id,
            reason="proxy_node_unreachable",
        )
        observation_count = session.query(ProxyExitIpObservation).count()

    assert new_binding.proxy_airport_node_id == backup_node.id
    assert new_binding.observed_exit_ip == ""
    assert observation_count == 0


def test_proxy_airport_failover_fails_closed_when_no_healthy_candidate() -> None:
    with _session() as session:
        primary = _add_subscription(session, name="primary", priority=10)
        old_node = _add_node(session, primary, "old-node", "unhealthy", "203.0.113.10")
        old_binding = _add_binding(session, old_node)
        session.commit()

        with pytest.raises(ValueError, match="airport_all_subscriptions_unavailable"):
            failover_proxy_airport_node_binding(
                session,
                tenant_id=1,
                proxy_binding_id=old_binding.id,
                reason="proxy_node_unreachable",
            )


def _same_subscription_values(
    old: AccountProxyBinding,
    new: AccountProxyBinding,
    event: ProxyNodeFailoverEvent,
    warmup: AccountProxyWarmupState,
    observation: ProxyExitIpObservation,
) -> dict[str, object]:
    return {
        "old_status": old.status,
        "old_unbound_at": old.unbound_at,
        "old_binding_id": old.id,
        "new_binding_id": new.id,
        "new_node_id": new.proxy_airport_node_id,
        "new_generation": new.binding_generation,
        "new_status": new.status,
        "event": (event.from_node_id, event.to_node_id, event.reason, event.outcome, event.observed_error),
        "warmup": (warmup.proxy_binding_id, warmup.stage, warmup.reset_reason),
        "observation": (observation.proxy_node_id, observation.proxy_binding_id, observation.observed_exit_ip),
    }


def _add_subscription(session: Session, *, name: str, priority: int) -> ProxyAirportSubscription:
    row = ProxyAirportSubscription(
        tenant_id=1,
        name=name,
        subscription_url_ciphertext="enc",
        subscription_url_preview=f"https://{name}.example.com/sub?...oken",
        priority=priority,
        enabled=True,
        sync_status="synced",
        healthy_node_count=1,
        node_count=1,
        is_active=True,
    )
    session.add(row)
    session.flush()
    return row


def _add_node(
    session: Session,
    subscription: ProxyAirportSubscription,
    name: str,
    status: str,
    observed_exit_ip: str,
) -> ProxyAirportNode:
    row = ProxyAirportNode(
        tenant_id=1,
        subscription_id=subscription.id,
        node_key=name,
        node_name=name,
        protocol="trojan",
        proxy_host=f"{name}.example.com",
        proxy_port=443,
        status=status,
        observed_exit_ip=observed_exit_ip,
        observed_exit_country="SG",
        observed_exit_asn="AS64500",
        observed_exit_isp="ExampleNet",
    )
    session.add(row)
    session.flush()
    return row


def _add_binding(session: Session, node: ProxyAirportNode) -> AccountProxyBinding:
    row = AccountProxyBinding(
        tenant_id=1,
        account_id=101,
        developer_app_id=11,
        developer_app_api_id_snapshot=10011,
        authorization_id=201,
        session_role="primary",
        proxy_airport_node_id=node.id,
        observed_exit_ip=node.observed_exit_ip,
        observed_exit_country=node.observed_exit_country,
        observed_exit_asn=node.observed_exit_asn,
        observed_exit_isp=node.observed_exit_isp,
        binding_generation=1,
        status="active",
    )
    session.add(row)
    session.flush()
    return row
