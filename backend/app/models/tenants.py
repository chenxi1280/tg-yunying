from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

from .enums import now


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    plan_name: Mapped[str] = mapped_column(String(80), default="试运行")
    account_quota: Mapped[int] = mapped_column(Integer, default=0)
    task_quota: Mapped[int] = mapped_column(Integer, default=5000)
    telegram_bot_token_ciphertext: Mapped[str] = mapped_column(Text, default="")
    admin_chat_id: Mapped[str] = mapped_column(String(120), default="")
    notify_ai_failures_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    group_rescue_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    group_rescue_admin_account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    group_rescue_bot_username: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    accounts: Mapped[list[TgAccount]] = relationship(back_populates="tenant", foreign_keys="TgAccount.tenant_id")
    groups: Mapped[list[TgGroup]] = relationship(back_populates="tenant")

    @property
    def telegram_bot_configured(self) -> bool:
        return bool(self.telegram_bot_token_ciphertext)


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
    role_template: Mapped[str] = mapped_column(String(40), default="运营管理员")
    email: Mapped[str] = mapped_column(String(160), unique=True)
    phone: Mapped[str | None] = mapped_column(String(40), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(240), default="")
    subscription_status: Mapped[str] = mapped_column(String(30), default="active")
    subscription_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    token_balance: Mapped[int] = mapped_column(Integer, default=0)
    token_quota_total: Mapped[int] = mapped_column(Integer, default=0)
    menu_permissions: Mapped[str] = mapped_column(Text, default="")
    permission_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_type: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    duration_days: Mapped[int] = mapped_column(Integer)
    token_quota: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ActivationCode(Base):
    __tablename__ = "activation_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("subscription_plans.id"), nullable=True)
    plan_type: Mapped[str] = mapped_column(String(30))
    plan_name: Mapped[str] = mapped_column(String(80), default="")
    duration_days: Mapped[int] = mapped_column(Integer)
    token_quota: Mapped[int] = mapped_column(Integer, default=0)
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
    plan: Mapped[SubscriptionPlan | None] = relationship()

    @property
    def redeemed_user_name(self) -> str | None:
        return self.redeemed_by_user.name if self.redeemed_by_user else None

    @property
    def redeemed_user_email(self) -> str | None:
        return self.redeemed_by_user.email if self.redeemed_by_user else None


class UserTokenLedger(Base):
    __tablename__ = "user_token_ledgers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"))
    change_type: Mapped[str] = mapped_column(String(40))
    delta_tokens: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    related_activation_code_id: Mapped[int | None] = mapped_column(ForeignKey("activation_codes.id"), nullable=True)
    related_ai_usage_ledger_id: Mapped[int | None] = mapped_column(ForeignKey("ai_usage_ledgers.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(255), default="")
    actor: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    user: Mapped[AppUser] = relationship()


__all__ = ["Tenant", "AccountPool", "AppUser", "SubscriptionPlan", "ActivationCode", "UserTokenLedger"]
