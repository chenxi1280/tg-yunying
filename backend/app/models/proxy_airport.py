from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


class ProxyAirportSubscription(Base):
    __tablename__ = "proxy_airport_subscriptions"
    __table_args__ = (UniqueConstraint("tenant_id", "is_active", name="uq_proxy_airport_active_subscription"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    subscription_url_ciphertext: Mapped[str] = mapped_column(Text, default="")
    subscription_url_preview: Mapped[str] = mapped_column(String(180), default="")
    provider_type: Mapped[str] = mapped_column(String(40), default="clash")
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
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = ["ProxyAirportNode", "ProxyAirportSubscription"]
