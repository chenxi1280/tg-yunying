from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

from .enums import GroupAuthStatus, now


class TgGroup(Base):
    __tablename__ = "tg_groups"
    __table_args__ = (UniqueConstraint("tenant_id", "tg_peer_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    tg_peer_id: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(160))
    group_type: Mapped[str] = mapped_column(String(40), default="supergroup")
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    auth_status: Mapped[str] = mapped_column(String(30), default=GroupAuthStatus.UNVERIFIED.value)
    can_send: Mapped[bool] = mapped_column(Boolean, default=True)
    active_window: Mapped[str] = mapped_column(String(80), default="09:00-23:00")
    daily_limit: Mapped[int] = mapped_column(Integer, default=120)
    account_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=180)
    group_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=60)
    topic_direction: Mapped[str] = mapped_column(String(200), default="日常讨论、活动答疑")
    banned_words: Mapped[str] = mapped_column(Text, default="")
    link_whitelist: Mapped[str] = mapped_column(Text, default="")
    require_review: Mapped[bool] = mapped_column(Boolean, default=True)

    tenant: Mapped[Tenant] = relationship(back_populates="groups")  # noqa: F821
    accounts: Mapped[list[TgGroupAccount]] = relationship(back_populates="group")  # noqa: F821


class TgGroupAccount(Base):
    __tablename__ = "tg_group_accounts"
    __table_args__ = (UniqueConstraint("group_id", "account_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("tg_groups.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    permission_label: Mapped[str] = mapped_column(String(80), default="普通成员")
    can_send: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # noqa: F821

    group: Mapped[TgGroup] = relationship(back_populates="accounts")
    account: Mapped[TgAccount] = relationship(back_populates="groups")  # noqa: F821


class VerificationTask(Base):
    __tablename__ = "verification_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("tg_groups.id"), nullable=True)
    message_task_id: Mapped[int | None] = mapped_column(ForeignKey("message_tasks.id"), nullable=True)
    verification_type: Mapped[str] = mapped_column(String(60), default="未知验证")
    detected_reason: Mapped[str] = mapped_column(Text, default="")
    suggested_action: Mapped[str] = mapped_column(String(120), default="人工处理")
    target_peer_id: Mapped[str] = mapped_column(String(120), default="")
    target_display: Mapped[str] = mapped_column(String(160), default="")
    requires_user_confirm: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(30), default="待处理")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)  # noqa: F821
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # noqa: F821


__all__ = ["TgGroup", "TgGroupAccount", "VerificationTask"]
