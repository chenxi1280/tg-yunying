from __future__ import annotations

from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ProxyAirportSubscription
from app.schemas.account_environment import ProxyAirportSubscriptionOut, ProxyAirportSubscriptionUpdate
from app.security import encrypt_secret
from app.services._common import _now, audit


def mask_subscription_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    path = parsed.path or "/"
    suffix = url[-4:] if len(url) >= 4 else url
    return f"{parsed.scheme}://{parsed.netloc}{path}?...{suffix}"


def get_proxy_airport_subscription(session: Session, *, tenant_id: int) -> ProxyAirportSubscriptionOut:
    row = _active_subscription(session, tenant_id)
    if row is None:
        return _empty_subscription(tenant_id)
    return _subscription_out(row)


def update_proxy_airport_subscription(
    session: Session,
    *,
    tenant_id: int,
    payload: ProxyAirportSubscriptionUpdate,
    actor: str,
) -> ProxyAirportSubscriptionOut:
    row = _active_subscription(session, tenant_id)
    if row is None:
        row = ProxyAirportSubscription(tenant_id=tenant_id, is_active=True)
        session.add(row)
    row.subscription_url_ciphertext = encrypt_secret(payload.subscription_url)
    row.subscription_url_preview = mask_subscription_url(payload.subscription_url)
    row.sync_status = "configured"
    row.last_error = ""
    row.updated_at = _now()
    session.flush()
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="保存全局 Clash 订阅",
        target_type="proxy_airport_subscription",
        target_id=str(row.id),
        detail=f"preview={row.subscription_url_preview}",
    )
    return _subscription_out(row)


def mark_proxy_airport_subscription_tested(session: Session, *, tenant_id: int, actor: str) -> ProxyAirportSubscriptionOut:
    row = _active_subscription(session, tenant_id)
    if row is None or not row.subscription_url_ciphertext:
        raise ValueError("clash_subscription_not_configured")
    row.sync_status = "test_pending"
    row.updated_at = _now()
    session.flush()
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="测试全局 Clash 订阅",
        target_type="proxy_airport_subscription",
        target_id=str(row.id),
        detail=f"status={row.sync_status}",
    )
    return _subscription_out(row)


def _active_subscription(session: Session, tenant_id: int) -> ProxyAirportSubscription | None:
    stmt = select(ProxyAirportSubscription).where(
        ProxyAirportSubscription.tenant_id == tenant_id,
        ProxyAirportSubscription.is_active.is_(True),
    )
    return session.scalar(stmt.order_by(ProxyAirportSubscription.id.desc()).limit(1))


def _empty_subscription(tenant_id: int) -> ProxyAirportSubscriptionOut:
    return ProxyAirportSubscriptionOut(
        id=None,
        tenant_id=tenant_id,
        subscription_url_configured=False,
        subscription_url_preview="",
        sync_status="not_configured",
        node_count=0,
        healthy_node_count=0,
    )


def _subscription_out(row: ProxyAirportSubscription) -> ProxyAirportSubscriptionOut:
    return ProxyAirportSubscriptionOut(
        id=row.id,
        tenant_id=row.tenant_id,
        subscription_url_configured=bool(row.subscription_url_ciphertext),
        subscription_url_preview=row.subscription_url_preview,
        provider_type=row.provider_type,
        sync_status=row.sync_status,
        node_count=row.node_count,
        healthy_node_count=row.healthy_node_count,
        last_sync_at=row.last_sync_at,
        last_error=row.last_error,
        updated_at=row.updated_at,
    )


__all__ = [
    "get_proxy_airport_subscription",
    "mark_proxy_airport_subscription_tested",
    "mask_subscription_url",
    "update_proxy_airport_subscription",
]
