from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


class ProxyAirportSubscription(Base):
    __tablename__ = "proxy_airport_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    name: Mapped[str] = mapped_column(String(80), default="主订阅")
    subscription_url_ciphertext: Mapped[str] = mapped_column(Text, default="")
    subscription_url_preview: Mapped[str] = mapped_column(String(180), default="")
    provider_type: Mapped[str] = mapped_column(String(40), default="clash")
    priority: Mapped[int] = mapped_column(Integer, default=10)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    failover_policy: Mapped[str] = mapped_column(String(40), default="same_subscription_first")
    auto_failback_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    failback_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    all_subscriptions_down_policy: Mapped[str] = mapped_column(String(40), default="pause_task")
    notify_admin_on_all_subscriptions_down: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_status: Mapped[str] = mapped_column(String(30), default="not_synced")
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    healthy_node_count: Mapped[int] = mapped_column(Integer, default=0)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ProxyAirportNode(Base):
    __tablename__ = "proxy_airport_nodes"
    __table_args__ = (UniqueConstraint("tenant_id", "subscription_id", "node_key", name="uq_proxy_airport_node_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("proxy_airport_subscriptions.id"))
    node_key: Mapped[str] = mapped_column(String(160), default="")
    node_name: Mapped[str] = mapped_column(String(160), default="")
    protocol: Mapped[str] = mapped_column(String(40), default="")
    proxy_host: Mapped[str] = mapped_column(String(255), default="")
    proxy_port: Mapped[int] = mapped_column(Integer, default=0)
    node_config_ciphertext: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="unknown")
    max_bound_accounts: Mapped[int] = mapped_column(Integer, default=5)
    observed_exit_ip: Mapped[str] = mapped_column(String(64), default="")
    observed_exit_country: Mapped[str] = mapped_column(String(16), default="")
    observed_exit_asn: Mapped[str] = mapped_column(String(80), default="")
    observed_exit_isp: Mapped[str] = mapped_column(String(120), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ProxyNodeFailoverEvent(Base):
    __tablename__ = "proxy_node_failover_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    developer_app_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    authorization_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_role: Mapped[str] = mapped_column(String(24), default="")
    from_subscription_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    to_subscription_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    from_node_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    to_node_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(80), default="")
    outcome: Mapped[str] = mapped_column(String(40), default="")
    observed_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProxyExitIpObservation(Base):
    __tablename__ = "proxy_exit_ip_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    proxy_node_id: Mapped[int | None] = mapped_column(ForeignKey("proxy_airport_nodes.id"), nullable=True)
    proxy_binding_id: Mapped[int | None] = mapped_column(ForeignKey("account_proxy_bindings.id"), nullable=True)
    observed_exit_ip: Mapped[str] = mapped_column(String(64), default="")
    observed_exit_country: Mapped[str] = mapped_column(String(16), default="")
    observed_exit_asn: Mapped[str] = mapped_column(String(80), default="")
    observed_exit_isp: Mapped[str] = mapped_column(String(120), default="")
    check_source: Mapped[str] = mapped_column(String(40), default="failover")
    raw_response: Mapped[str] = mapped_column(Text, default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "ProxyAirportNode",
    "ProxyAirportSubscription",
    "ProxyExitIpObservation",
    "ProxyNodeFailoverEvent",
]
