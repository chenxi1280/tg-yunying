from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def new_uuid() -> str:
    return str(uuid4())


class AccountEnvironmentBinding(Base):
    __tablename__ = "account_environment_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "authorization_id", "session_role", name="uq_account_environment_authorization_role"),
        Index("ix_account_environment_account", "tenant_id", "account_id", "status"),
        Index("ix_account_environment_identity", "tenant_id", "client_identity_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    authorization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_authorizations.id"))
    session_role: Mapped[str] = mapped_column(String(24), default="primary")
    proxy_binding_id: Mapped[int | None] = mapped_column(ForeignKey("account_proxy_bindings.id"), nullable=True)
    proxy_id: Mapped[int | None] = mapped_column(ForeignKey("account_proxies.id"), nullable=True)
    device_model: Mapped[str] = mapped_column(String(120), default="")
    system_version: Mapped[str] = mapped_column(String(80), default="")
    app_version: Mapped[str] = mapped_column(String(60), default="")
    platform: Mapped[str] = mapped_column(String(40), default="")
    lang_code: Mapped[str] = mapped_column(String(16), default="zh")
    system_lang_code: Mapped[str] = mapped_column(String(16), default="zh-CN")
    lang_pack: Mapped[str] = mapped_column(String(40), default="")
    region_code: Mapped[str] = mapped_column(String(16), default="CN")
    client_identity_key: Mapped[str] = mapped_column(String(160), default="")
    fingerprint_locked: Mapped[bool] = mapped_column(Boolean, default=True)
    health_score: Mapped[int] = mapped_column(Integer, default=100)
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    unbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FingerprintComboHistory(Base):
    __tablename__ = "fingerprint_combo_history"
    __table_args__ = (
        UniqueConstraint("tenant_id", "combo_key", name="uq_fingerprint_combo_history_key"),
        Index("ix_fingerprint_combo_history_account", "tenant_id", "account_id", "authorization_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    authorization_id: Mapped[int | None] = mapped_column(ForeignKey("tg_account_authorizations.id"), nullable=True)
    session_role: Mapped[str] = mapped_column(String(24), default="")
    combo_key: Mapped[str] = mapped_column(String(160), default="")
    device_model: Mapped[str] = mapped_column(String(120), default="")
    system_version: Mapped[str] = mapped_column(String(80), default="")
    app_version: Mapped[str] = mapped_column(String(60), default="")
    platform: Mapped[str] = mapped_column(String(40), default="")
    lang_code: Mapped[str] = mapped_column(String(16), default="zh")
    system_lang_code: Mapped[str] = mapped_column(String(16), default="zh-CN")
    region_code: Mapped[str] = mapped_column(String(16), default="CN")
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    first_bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    status: Mapped[str] = mapped_column(String(30), default="active")


__all__ = ["AccountEnvironmentBinding", "FingerprintComboHistory"]
