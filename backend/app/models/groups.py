from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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
    send_limit_mode: Mapped[str] = mapped_column(String(60), default="legacy_group_slot")
    topic_direction: Mapped[str] = mapped_column(String(200), default="日常讨论、活动答疑")
    banned_words: Mapped[str] = mapped_column(Text, default="")
    link_whitelist: Mapped[str] = mapped_column(Text, default="")
    require_review: Mapped[bool] = mapped_column(Boolean, default=True)
    listener_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    listener_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    listener_context_limit: Mapped[int] = mapped_column(Integer, default=20)
    listener_auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    listener_last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    listener_last_reply_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    listener_last_error: Mapped[str] = mapped_column(Text, default="")

    tenant: Mapped[Tenant] = relationship(back_populates="groups")
    accounts: Mapped[list[TgGroupAccount]] = relationship(back_populates="group")

    @property
    def listener_account_ids(self) -> list[int]:
        return [link.account_id for link in self.accounts if link.is_listener]


class TgGroupAccount(Base):
    __tablename__ = "tg_group_accounts"
    __table_args__ = (UniqueConstraint("group_id", "account_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("tg_groups.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    permission_label: Mapped[str] = mapped_column(String(80), default="普通成员")
    can_send: Mapped[bool] = mapped_column(Boolean, default=True)
    is_listener: Mapped[bool] = mapped_column(Boolean, default=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    group: Mapped[TgGroup] = relationship(back_populates="accounts")
    account: Mapped[TgAccount] = relationship(back_populates="groups")


class GroupContextMessage(Base):
    __tablename__ = "group_context_messages"
    __table_args__ = (
        UniqueConstraint("group_id", "remote_message_id"),
        Index("ix_group_context_messages_tenant_group_recent", "tenant_id", "group_id", "sent_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("tg_groups.id"))
    listener_account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    sender_peer_id: Mapped[str] = mapped_column(String(120), default="")
    sender_name: Mapped[str] = mapped_column(String(160), default="真人用户")
    sender_username: Mapped[str] = mapped_column(String(120), default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    sender_role: Mapped[str] = mapped_column(String(40), default="member")
    content: Mapped[str] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(String(40), default="text")
    remote_message_id: Mapped[str] = mapped_column(String(160))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    used_for_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def issue_scope(self) -> str:
        if self.group_id:
            return "target"
        return "account"

    @property
    def issue_category(self) -> str:
        text = f"{self.verification_type} {self.detected_reason} {self.suggested_action}"
        if self.can_auto_resolve:
            return "verification"
        if self.group_id and any(keyword in text for keyword in ("群", "发言", "验证", "按钮", "关注", "慢速", "权限")):
            return "group_restriction"
        if any(keyword in text for keyword in ("账号受限", "Session", "重新登录", "不可用")):
            return "account_restricted"
        return "verification"

    @property
    def can_auto_resolve(self) -> bool:
        return self.suggested_action in {"关注频道", "点击按钮", "发送验证回复", "识别图形验证码"}

    @property
    def requires_target_recheck(self) -> bool:
        return self.issue_category == "group_restriction"

    @property
    def resolution_entry_label(self) -> str:
        if self.issue_category == "group_restriction":
            return "解除群限制"
        if self.can_auto_resolve:
            return "执行自动处理"
        return "处理验证辅助"


__all__ = ["TgGroup", "TgGroupAccount", "GroupContextMessage", "VerificationTask"]
