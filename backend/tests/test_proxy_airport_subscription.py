from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import ProxyAirportNode, Tenant
from app.schemas.account_environment import ProxyAirportSubscriptionUpdate
from app.security import decrypt_secret
from app.services.proxy_airport_subscription import (
    get_proxy_airport_subscription,
    mask_subscription_url,
    sync_proxy_airport_subscription,
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
