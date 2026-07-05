from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    ProxyAirportNode,
    Tenant,
)
from app.schemas.account_environment import (
    ProxyAirportSubscriptionCreate,
    ProxyAirportSubscriptionPatch,
    ProxyAirportSubscriptionUpdate,
)
from app.security import decrypt_secret
from app.services.proxy_airport_subscription import (
    create_proxy_airport_subscription,
    get_proxy_airport_subscription,
    list_proxy_airport_subscriptions,
    mask_subscription_url,
    patch_proxy_airport_subscription,
    select_proxy_airport_subscription_for_failover,
    sync_proxy_airport_subscription,
    sync_proxy_airport_subscription_by_id,
    update_proxy_airport_subscription,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    return session


def _save_subscription(session: Session) -> None:
    update_proxy_airport_subscription(
        session,
        tenant_id=1,
        payload=ProxyAirportSubscriptionUpdate(subscription_url="https://example.com/sub?token=secret-token"),
        actor="tester",
    )


def test_proxy_airport_subscription_masks_url_and_encrypts_raw_value() -> None:
    with _session() as session:
        _save_subscription(session)
        session.commit()

        loaded = get_proxy_airport_subscription(session, tenant_id=1)

    assert loaded.subscription_url_configured is True
    assert loaded.subscription_url_preview == "https://example.com/sub?...oken"
    assert "secret-token" not in loaded.model_dump_json()


def test_mask_subscription_url_never_returns_full_token() -> None:
    assert mask_subscription_url("https://xsus.example/sub/path?token=example-secret-token") == "https://xsus.example/sub/path?...oken"


def test_proxy_airport_subscription_sync_parses_nodes() -> None:
    with _session() as session:
        _save_subscription(session)

        synced = sync_proxy_airport_subscription(
            session,
            tenant_id=1,
            actor="tester",
            fetcher=lambda _url: "trojan://secret@example.com:443?sni=example.com#hk-1\nanytls://pass@edge.example.net:8443#sg-1",
        )
        nodes = list(session.scalars(select(ProxyAirportNode).order_by(ProxyAirportNode.node_key)))

    assert synced.sync_status == "synced"
    assert synced.node_count == 2
    assert synced.healthy_node_count == 0
    assert synced.last_error == ""
    assert [(node.node_name, node.protocol, node.proxy_host, node.proxy_port, node.status) for node in nodes] == [
        ("hk-1", "trojan", "example.com", 443, "unknown"),
        ("sg-1", "anytls", "edge.example.net", 8443, "unknown"),
    ]
    assert "secret" not in nodes[0].node_config_ciphertext
    assert decrypt_secret(nodes[0].node_config_ciphertext)


def test_proxy_airport_subscription_sync_applies_health_probe_results() -> None:
    with _session() as session:
        _save_subscription(session)

        synced = sync_proxy_airport_subscription(
            session,
            tenant_id=1,
            actor="tester",
            fetcher=lambda _url: "trojan://secret@example.com:443#hk-1\nanytls://pass@edge.example.net:8443#sg-1",
            health_checker=lambda node: (node.node_name == "hk-1", "" if node.node_name == "hk-1" else "connect_timeout"),
        )
        nodes = list(session.scalars(select(ProxyAirportNode).order_by(ProxyAirportNode.node_name)))

    assert synced.sync_status == "synced"
    assert synced.node_count == 2
    assert synced.healthy_node_count == 1
    assert [(node.node_name, node.status, node.last_error) for node in nodes] == [
        ("hk-1", "healthy", ""),
        ("sg-1", "unhealthy", "connect_timeout"),
    ]


def test_proxy_airport_subscription_sync_records_visible_failure() -> None:
    with _session() as session:
        _save_subscription(session)

        with pytest.raises(ValueError, match="no_supported_proxy_nodes"):
            sync_proxy_airport_subscription(
                session,
                tenant_id=1,
                actor="tester",
                fetcher=lambda _url: "traffic package expires soon",
            )
        loaded = get_proxy_airport_subscription(session, tenant_id=1)

    assert loaded.sync_status == "failed"
    assert loaded.node_count == 0
    assert loaded.last_error == "no_supported_proxy_nodes"


def test_proxy_airport_subscription_save_new_url_clears_old_nodes() -> None:
    with _session() as session:
        _save_subscription(session)
        sync_proxy_airport_subscription(
            session,
            tenant_id=1,
            actor="tester",
            fetcher=lambda _url: "trojan://secret@example.com:443#hk-1",
        )

        saved = update_proxy_airport_subscription(
            session,
            tenant_id=1,
            payload=ProxyAirportSubscriptionUpdate(subscription_url="https://example.net/new?token=second-token"),
            actor="tester",
        )
        nodes = list(session.scalars(select(ProxyAirportNode)))

    assert saved.sync_status == "configured"
    assert saved.node_count == 0
    assert saved.healthy_node_count == 0
    assert saved.last_sync_at is None
    assert nodes == []


def test_proxy_airport_subscription_sync_records_fetch_failure() -> None:
    with _session() as session:
        _save_subscription(session)

        with pytest.raises(ValueError, match="proxy_airport_subscription_fetch_failed"):
            sync_proxy_airport_subscription(
                session,
                tenant_id=1,
                actor="tester",
                fetcher=lambda _url: (_ for _ in ()).throw(OSError("network down")),
            )
        loaded = get_proxy_airport_subscription(session, tenant_id=1)

    assert loaded.sync_status == "failed"
    assert loaded.node_count == 0
    assert loaded.last_error == "proxy_airport_subscription_fetch_failed"


def test_proxy_airport_subscription_sync_accepts_json_node_array() -> None:
    with _session() as session:
        _save_subscription(session)

        synced = sync_proxy_airport_subscription(
            session,
            tenant_id=1,
            actor="tester",
            fetcher=lambda _url: '[{"name":"json-trojan","type":"trojan","server":"json.example.com","port":443},{"name":"剩余流量","type":"trojan","server":"traffic.example.com","port":443}]',
        )
        nodes = list(session.scalars(select(ProxyAirportNode).order_by(ProxyAirportNode.node_key)))

    assert synced.sync_status == "synced"
    assert synced.node_count == 1
    assert [(node.node_name, node.protocol, node.proxy_host, node.proxy_port) for node in nodes] == [("json-trojan", "trojan", "json.example.com", 443)]


def test_proxy_airport_subscription_pool_lists_enabled_sources_by_priority() -> None:
    with _session() as session:
        create_proxy_airport_subscription(
            session,
            tenant_id=1,
            payload=ProxyAirportSubscriptionCreate(
                name="backup",
                subscription_url="https://backup.example.com/sub?token=backup-secret",
                priority=20,
                enabled=True,
            ),
            actor="tester",
        )
        create_proxy_airport_subscription(
            session,
            tenant_id=1,
            payload=ProxyAirportSubscriptionCreate(
                name="primary",
                subscription_url="https://primary.example.com/sub?token=primary-secret",
                priority=10,
                enabled=True,
            ),
            actor="tester",
        )

        rows = list_proxy_airport_subscriptions(session, tenant_id=1)

    assert [row.name for row in rows] == ["primary", "backup"]
    assert [row.priority for row in rows] == [10, 20]
    assert all(row.subscription_url_configured for row in rows)
    assert "primary-secret" not in rows[0].model_dump_json()


def test_proxy_airport_subscription_defaults_match_same_subscription_first_failover_policy() -> None:
    payload = ProxyAirportSubscriptionCreate(
        name="primary",
        subscription_url="https://primary.example.com/sub",
    )

    assert payload.failover_policy == "same_subscription_first"
    assert payload.auto_failback_enabled is False
    assert payload.failback_cooldown_minutes == 1440


def test_proxy_airport_subscription_rejects_auto_failback_until_runtime_exists() -> None:
    with pytest.raises(ValueError, match="proxy_airport_auto_failback_not_implemented"):
        ProxyAirportSubscriptionCreate(
            name="primary",
            subscription_url="https://primary.example.com/sub",
            auto_failback_enabled=True,
        )


def test_proxy_airport_subscription_pool_rejects_duplicate_enabled_priority() -> None:
    with _session() as session:
        create_proxy_airport_subscription(
            session,
            tenant_id=1,
            payload=ProxyAirportSubscriptionCreate(
                name="primary",
                subscription_url="https://primary.example.com/sub",
                priority=10,
                enabled=True,
            ),
            actor="tester",
        )

        with pytest.raises(ValueError, match="proxy_airport_subscription_priority_conflict"):
            create_proxy_airport_subscription(
                session,
                tenant_id=1,
                payload=ProxyAirportSubscriptionCreate(
                    name="backup",
                    subscription_url="https://backup.example.com/sub",
                    priority=10,
                    enabled=True,
                ),
                actor="tester",
            )


def test_proxy_airport_subscription_sync_failure_is_scoped_to_that_subscription() -> None:
    with _session() as session:
        primary = create_proxy_airport_subscription(
            session,
            tenant_id=1,
            payload=ProxyAirportSubscriptionCreate(
                name="primary",
                subscription_url="https://primary.example.com/sub",
                priority=10,
                enabled=True,
            ),
            actor="tester",
        )
        backup = create_proxy_airport_subscription(
            session,
            tenant_id=1,
            payload=ProxyAirportSubscriptionCreate(
                name="backup",
                subscription_url="https://backup.example.com/sub",
                priority=20,
                enabled=True,
            ),
            actor="tester",
        )
        sync_proxy_airport_subscription_by_id(
            session,
            tenant_id=1,
            subscription_id=primary.id or 0,
            actor="tester",
            fetcher=lambda _url: "trojan://secret@primary.example.com:443#primary-node",
            health_checker=lambda _node: (True, ""),
        )

        with pytest.raises(ValueError, match="no_supported_proxy_nodes"):
            sync_proxy_airport_subscription_by_id(
                session,
                tenant_id=1,
                subscription_id=backup.id or 0,
                actor="tester",
                fetcher=lambda _url: "traffic package expires soon",
            )
        rows = list_proxy_airport_subscriptions(session, tenant_id=1)
        nodes = list(session.scalars(select(ProxyAirportNode).order_by(ProxyAirportNode.node_name)))

    assert [(row.name, row.sync_status, row.healthy_node_count) for row in rows] == [
        ("primary", "synced", 1),
        ("backup", "failed", 0),
    ]
    assert [(node.subscription_id, node.node_name) for node in nodes] == [(primary.id, "primary-node")]


def test_proxy_airport_subscription_failover_selects_first_enabled_healthy_source() -> None:
    with _session() as session:
        primary = create_proxy_airport_subscription(
            session,
            tenant_id=1,
            payload=ProxyAirportSubscriptionCreate(
                name="primary",
                subscription_url="https://primary.example.com/sub",
                priority=10,
                enabled=True,
            ),
            actor="tester",
        )
        backup = create_proxy_airport_subscription(
            session,
            tenant_id=1,
            payload=ProxyAirportSubscriptionCreate(
                name="backup",
                subscription_url="https://backup.example.com/sub",
                priority=20,
                enabled=True,
            ),
            actor="tester",
        )
        sync_proxy_airport_subscription_by_id(
            session,
            tenant_id=1,
            subscription_id=primary.id or 0,
            actor="tester",
            fetcher=lambda _url: "trojan://secret@primary.example.com:443#primary-node",
            health_checker=lambda _node: (False, "connect_timeout"),
        )
        sync_proxy_airport_subscription_by_id(
            session,
            tenant_id=1,
            subscription_id=backup.id or 0,
            actor="tester",
            fetcher=lambda _url: "trojan://secret@backup.example.com:443#backup-node",
            health_checker=lambda _node: (True, ""),
        )

        selected = select_proxy_airport_subscription_for_failover(session, tenant_id=1)

    assert selected is not None
    assert selected.name == "backup"
    assert selected.id == backup.id


def test_proxy_airport_subscription_disabled_sources_do_not_block_priority_reuse() -> None:
    with _session() as session:
        disabled = create_proxy_airport_subscription(
            session,
            tenant_id=1,
            payload=ProxyAirportSubscriptionCreate(
                name="old-primary",
                subscription_url="https://old.example.com/sub",
                priority=10,
                enabled=False,
            ),
            actor="tester",
        )

        enabled = patch_proxy_airport_subscription(
            session,
            tenant_id=1,
            subscription_id=disabled.id or 0,
            payload=ProxyAirportSubscriptionPatch(enabled=True),
            actor="tester",
        )

    assert enabled.enabled is True
