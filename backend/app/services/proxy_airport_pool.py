from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ProxyAirportSubscription
from app.schemas.account_environment import (
    ProxyAirportSubscriptionCreate,
    ProxyAirportSubscriptionOut,
    ProxyAirportSubscriptionPatch,
)
from app.security import decrypt_secret, encrypt_secret
from app.services._common import _now, audit

from .proxy_airport_subscription import (
    NodeHealthChecker,
    SubscriptionFetcher,
    _apply_node_health_checks,
    _delete_subscription_nodes,
    _healthy_node_count,
    _record_sync_failure,
    _replace_subscription_nodes,
    _subscription_nodes,
    _subscription_out,
    fetch_subscription,
    mask_subscription_url,
    parsed_proxy_nodes,
)


def list_proxy_airport_subscriptions(session: Session, *, tenant_id: int) -> list[ProxyAirportSubscriptionOut]:
    rows = session.scalars(_pool_query(tenant_id)).all()
    return [_subscription_out(row) for row in rows]


def create_proxy_airport_subscription(
    session: Session,
    *,
    tenant_id: int,
    payload: ProxyAirportSubscriptionCreate,
    actor: str,
) -> ProxyAirportSubscriptionOut:
    _ensure_priority_available(session, tenant_id=tenant_id, priority=payload.priority, enabled=payload.enabled)
    row = ProxyAirportSubscription(
        tenant_id=tenant_id,
        name=payload.name,
        subscription_url_ciphertext=encrypt_secret(payload.subscription_url),
        subscription_url_preview=mask_subscription_url(payload.subscription_url),
        priority=payload.priority,
        enabled=payload.enabled,
        failover_policy=payload.failover_policy,
        auto_failback_enabled=payload.auto_failback_enabled,
        failback_cooldown_minutes=payload.failback_cooldown_minutes,
        all_subscriptions_down_policy=payload.all_subscriptions_down_policy,
        notify_admin_on_all_subscriptions_down=payload.notify_admin_on_all_subscriptions_down,
        sync_status="configured",
        is_active=payload.enabled,
    )
    session.add(row)
    session.flush()
    _audit_subscription(session, actor, row, "新增 Clash 订阅源")
    return _subscription_out(row)


def patch_proxy_airport_subscription(
    session: Session,
    *,
    tenant_id: int,
    subscription_id: int,
    payload: ProxyAirportSubscriptionPatch,
    actor: str,
) -> ProxyAirportSubscriptionOut:
    row = _subscription_by_id(session, tenant_id=tenant_id, subscription_id=subscription_id)
    next_priority = payload.priority if payload.priority is not None else row.priority
    next_enabled = payload.enabled if payload.enabled is not None else row.enabled
    _ensure_priority_available(
        session,
        tenant_id=tenant_id,
        priority=next_priority,
        enabled=next_enabled,
        exclude_id=row.id,
    )
    _apply_patch(row, payload)
    row.updated_at = _now()
    session.flush()
    _audit_subscription(session, actor, row, "更新 Clash 订阅源")
    return _subscription_out(row)


def sync_proxy_airport_subscription_by_id(
    session: Session,
    *,
    tenant_id: int,
    subscription_id: int,
    actor: str,
    fetcher: SubscriptionFetcher | None = None,
    health_checker: NodeHealthChecker | None = None,
) -> ProxyAirportSubscriptionOut:
    row = _subscription_by_id(session, tenant_id=tenant_id, subscription_id=subscription_id)
    if not row.subscription_url_ciphertext:
        raise ValueError("clash_subscription_not_configured")
    try:
        raw = (fetcher or fetch_subscription)(_decrypt_subscription_url(row))
        nodes = parsed_proxy_nodes(raw)
        _replace_subscription_nodes(session, row, nodes)
        session.flush()
        node_rows = _subscription_nodes(session, row)
        if health_checker is not None:
            _apply_node_health_checks(node_rows, health_checker)
        _record_sync_success(row, len(node_rows), _healthy_node_count(node_rows))
        session.flush()
    except ValueError as exc:
        _record_sync_failure(session, row, actor, str(exc))
        raise
    except OSError as exc:
        _record_sync_failure(session, row, actor, "proxy_airport_subscription_fetch_failed")
        raise ValueError("proxy_airport_subscription_fetch_failed") from exc
    _audit_subscription(session, actor, row, "同步 Clash 订阅源")
    return _subscription_out(row)


def select_proxy_airport_subscription_for_failover(
    session: Session,
    *,
    tenant_id: int,
) -> ProxyAirportSubscriptionOut | None:
    stmt = _pool_query(tenant_id).where(
        ProxyAirportSubscription.enabled.is_(True),
        ProxyAirportSubscription.sync_status == "synced",
        ProxyAirportSubscription.healthy_node_count > 0,
    )
    row = session.scalar(stmt.limit(1))
    return _subscription_out(row) if row is not None else None


def _apply_patch(row: ProxyAirportSubscription, payload: ProxyAirportSubscriptionPatch) -> None:
    if payload.name is not None:
        row.name = payload.name
    if payload.priority is not None:
        row.priority = payload.priority
    if payload.enabled is not None:
        row.enabled = payload.enabled
        row.is_active = payload.enabled
    if payload.failover_policy is not None:
        row.failover_policy = payload.failover_policy
    if payload.auto_failback_enabled is not None:
        row.auto_failback_enabled = payload.auto_failback_enabled
    if payload.failback_cooldown_minutes is not None:
        row.failback_cooldown_minutes = payload.failback_cooldown_minutes
    if payload.all_subscriptions_down_policy is not None:
        row.all_subscriptions_down_policy = payload.all_subscriptions_down_policy
    if payload.notify_admin_on_all_subscriptions_down is not None:
        row.notify_admin_on_all_subscriptions_down = payload.notify_admin_on_all_subscriptions_down
    if payload.subscription_url is not None:
        row.subscription_url_ciphertext = encrypt_secret(payload.subscription_url)
        row.subscription_url_preview = mask_subscription_url(payload.subscription_url)
        row.sync_status = "configured"
        row.node_count = 0
        row.healthy_node_count = 0
        row.last_sync_at = None
        row.last_error = ""


def _ensure_priority_available(
    session: Session,
    *,
    tenant_id: int,
    priority: int,
    enabled: bool,
    exclude_id: int | None = None,
) -> None:
    if not enabled:
        return
    stmt = select(ProxyAirportSubscription.id).where(
        ProxyAirportSubscription.tenant_id == tenant_id,
        ProxyAirportSubscription.enabled.is_(True),
        ProxyAirportSubscription.priority == priority,
    )
    if exclude_id is not None:
        stmt = stmt.where(ProxyAirportSubscription.id != exclude_id)
    if session.scalar(stmt.limit(1)) is not None:
        raise ValueError("proxy_airport_subscription_priority_conflict")


def _subscription_by_id(session: Session, *, tenant_id: int, subscription_id: int) -> ProxyAirportSubscription:
    row = session.get(ProxyAirportSubscription, subscription_id)
    if row is None or row.tenant_id != tenant_id:
        raise ValueError("proxy_airport_subscription_not_found")
    return row


def _pool_query(tenant_id: int):
    return (
        select(ProxyAirportSubscription)
        .where(ProxyAirportSubscription.tenant_id == tenant_id)
        .order_by(ProxyAirportSubscription.priority, ProxyAirportSubscription.id)
    )


def _record_sync_success(row: ProxyAirportSubscription, node_count: int, healthy_node_count: int) -> None:
    row.sync_status = "synced"
    row.node_count = node_count
    row.healthy_node_count = healthy_node_count
    row.last_error = ""
    row.last_sync_at = _now()
    row.updated_at = _now()


def _decrypt_subscription_url(row: ProxyAirportSubscription) -> str:
    url = decrypt_secret(row.subscription_url_ciphertext) or ""
    if not url:
        raise ValueError("clash_subscription_not_configured")
    return url


def _audit_subscription(session: Session, actor: str, row: ProxyAirportSubscription, action: str) -> None:
    audit(
        session,
        tenant_id=row.tenant_id,
        actor=actor,
        action=action,
        target_type="proxy_airport_subscription",
        target_id=str(row.id),
        detail=f"name={row.name}; priority={row.priority}; enabled={row.enabled}",
    )
