from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

from .enums import now


class AccountProxy(Base):
    __tablename__ = "account_proxies"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    name: Mapped[str] = mapped_column(String(120))
    protocol: Mapped[str] = mapped_column(String(16), default="socks5")
    host: Mapped[str] = mapped_column(String(120), default="127.0.0.1")
    port: Mapped[int] = mapped_column(Integer)
    username: Mapped[str] = mapped_column(String(120), default="")
    password_ciphertext: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="unknown")
    alert_status: Mapped[str] = mapped_column(String(30), default="normal")
    check_interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=3000)
    max_bound_accounts: Mapped[int] = mapped_column(Integer, default=5)
    max_concurrent_sessions: Mapped[int] = mapped_column(Integer, default=2)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    disabled_reason: Mapped[str] = mapped_column(String(255), default="")
    notes: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    accounts: Mapped[list[TgAccount]] = relationship(back_populates="proxy")

    @property
    def local_address(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}"


class AccountProxyBinding(Base):
    __tablename__ = "account_proxy_bindings"
    __table_args__ = (
        Index(
            "uq_account_proxy_binding_active_slot",
            "tenant_id",
            "account_id",
            "developer_app_id",
            "authorization_id",
            "session_role",
            unique=True,
            sqlite_where=text("status = 'active' AND unbound_at IS NULL AND developer_app_id IS NOT NULL AND authorization_id IS NOT NULL AND session_role != ''"),
            postgresql_where=text("status = 'active' AND unbound_at IS NULL AND developer_app_id IS NOT NULL AND authorization_id IS NOT NULL AND session_role != ''"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    developer_app_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_developer_apps.id"), nullable=True)
    developer_app_api_id_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    authorization_id: Mapped[int | None] = mapped_column(ForeignKey("tg_account_authorizations.id"), nullable=True)
    session_role: Mapped[str] = mapped_column(String(24), default="")
    proxy_id: Mapped[int | None] = mapped_column(ForeignKey("account_proxies.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="active")
    change_reason: Mapped[str] = mapped_column(String(255), default="")
    bound_by: Mapped[str] = mapped_column(String(100), default="")
    bound_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    unbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ProxyAlert(Base):
    __tablename__ = "proxy_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    proxy_id: Mapped[int] = mapped_column(ForeignKey("account_proxies.id"))
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    status: Mapped[str] = mapped_column(String(30), default="alerting")
    alert_type: Mapped[str] = mapped_column(String(60), default="manual")
    reason_code: Mapped[str] = mapped_column(String(80), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_by: Mapped[str] = mapped_column(String(100), default="")
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ignored_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    affected_account_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    related_risk_event_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    suggested_action: Mapped[str] = mapped_column(String(255), default="")
    audit_id: Mapped[str] = mapped_column(String(80), default="")

    proxy: Mapped[AccountProxy] = relationship()


class ProxyHealthCheck(Base):
    __tablename__ = "proxy_health_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    proxy_id: Mapped[int] = mapped_column(ForeignKey("account_proxies.id"))
    check_type: Mapped[str] = mapped_column(String(40), default="port_connect")
    status: Mapped[str] = mapped_column(String(30), default="unknown")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str] = mapped_column(String(80), default="")
    error_detail: Mapped[str] = mapped_column(Text, default="")
    checked_by: Mapped[str] = mapped_column(String(100), default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    trace_id: Mapped[str] = mapped_column(String(80), default="")


__all__ = ["AccountProxy", "AccountProxyBinding", "ProxyAlert", "ProxyHealthCheck"]
