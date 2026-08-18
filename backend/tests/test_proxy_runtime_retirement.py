from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
import pytest

from app.database import Base
from app.models import AccountProxy, AccountProxyBinding, AuditLog
from app.services.proxy_runtime_retirement import (
    ProxyRetirementRequest,
    retire_proxy_runtimes,
    snapshot_proxy_runtimes,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _proxy(proxy_id: int, name: str) -> AccountProxy:
    return AccountProxy(
        id=proxy_id,
        tenant_id=1,
        name=name,
        protocol="socks5",
        host=name,
        port=7890,
        status="healthy",
        alert_status="normal",
    )


def test_zero_consumer_proxy_is_disabled_with_audit() -> None:
    session = _session()
    proxy = _proxy(1, "tgyunying-mihomo-018")
    session.add(proxy)
    session.commit()
    record = snapshot_proxy_runtimes(session, (proxy.name,))[0]

    retired = retire_proxy_runtimes(
        session,
        ProxyRetirementRequest(
            expected_state_hashes={proxy.name: record.state_hash()},
            actor="prod-resource-repair",
            approval_ref="planner-memory-prd-20260818",
        ),
    )
    session.commit()

    assert [item.name for item in retired] == [proxy.name]
    assert session.get(AccountProxy, proxy.id).status == "disabled"
    audit = session.scalar(select(AuditLog).where(AuditLog.target_id == str(proxy.id)))
    assert audit is not None
    assert "planner-memory-prd-20260818" in audit.detail


def test_active_binding_blocks_retirement() -> None:
    session = _session()
    proxy = _proxy(2, "tgyunying-mihomo-019")
    session.add(proxy)
    session.add(
        AccountProxyBinding(
            tenant_id=1,
            account_id=999,
            proxy_id=proxy.id,
            status="active",
            unbound_at=None,
        )
    )
    session.commit()
    record = snapshot_proxy_runtimes(session, (proxy.name,))[0]

    try:
        retire_proxy_runtimes(
            session,
            ProxyRetirementRequest(
                expected_state_hashes={proxy.name: record.state_hash()},
                actor="prod-resource-repair",
                approval_ref="planner-memory-prd-20260818",
            ),
        )
    except RuntimeError as exc:
        assert str(exc) == f"proxy_runtime_has_consumers:{proxy.name}"
    else:
        raise AssertionError("active proxy binding must block retirement")

    assert session.get(AccountProxy, proxy.id).status == "healthy"


def test_state_hash_drift_blocks_retirement() -> None:
    session = _session()
    proxy = _proxy(3, "tgyunying-mihomo-021")
    session.add(proxy)
    session.commit()
    record = snapshot_proxy_runtimes(session, (proxy.name,))[0]
    proxy.notes = "changed after preview"
    proxy.status = "unhealthy"
    session.commit()

    try:
        retire_proxy_runtimes(
            session,
            ProxyRetirementRequest(
                expected_state_hashes={proxy.name: record.state_hash()},
                actor="prod-resource-repair",
                approval_ref="planner-memory-prd-20260818",
            ),
        )
    except RuntimeError as exc:
        assert str(exc) == f"proxy_runtime_state_hash_mismatch:{proxy.name}"
    else:
        raise AssertionError("state drift must block retirement")
