from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

from .enums import now


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    plan_name: Mapped[str] = mapped_column(String(80), default="试运行")
    account_quota: Mapped[int] = mapped_column(Integer, default=50)
    task_quota: Mapped[int] = mapped_column(Integer, default=5000)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    accounts: Mapped[list[TgAccount]] = relationship(back_populates="tenant")
    groups: Mapped[list[TgGroup]] = relationship(back_populates="tenant")


class AccountPool(Base):
    __tablename__ = "account_pools"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(255), default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    accounts: Mapped[list[TgAccount]] = relationship(back_populates="pool")


class AppUser(Base):
    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(40))
    email: Mapped[str] = mapped_column(String(160), unique=True)
    phone: Mapped[str | None] = mapped_column(String(40), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(240), default="")
    subscription_status: Mapped[str] = mapped_column(String(30), default="active")
    subscription_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ActivationCode(Base):
    __tablename__ = "activation_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    plan_type: Mapped[str] = mapped_column(String(30))
    duration_days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="unused")
    batch_no: Mapped[str] = mapped_column(String(24), default="")
    serial_prefix: Mapped[str] = mapped_column(String(24), default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    redeemed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_users.id"), nullable=True)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    subscription_start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    subscription_end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(String(255), default="")

    redeemed_by_user: Mapped[AppUser | None] = relationship()

    @property
    def redeemed_user_name(self) -> str | None:
        return self.redeemed_by_user.name if self.redeemed_by_user else None

    @property
    def redeemed_user_email(self) -> str | None:
        return self.redeemed_by_user.email if self.redeemed_by_user else None


__all__ = ["Tenant", "AccountPool", "AppUser", "ActivationCode"]
