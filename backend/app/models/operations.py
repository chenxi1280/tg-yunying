from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import TaskStatus, now


class OperationTarget(Base):
    __tablename__ = "operation_targets"
    __table_args__ = (UniqueConstraint("tenant_id", "tg_peer_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    target_type: Mapped[str] = mapped_column(String(20), default="group")
    tg_peer_id: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(180))
    username: Mapped[str] = mapped_column(String(120), default="")
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    can_send: Mapped[bool] = mapped_column(Boolean, default=True)
    auth_status: Mapped[str] = mapped_column(String(30), default="未确认")
    lifecycle_status: Mapped[str] = mapped_column(String(40), default="active")
    lifecycle_reason: Mapped[str] = mapped_column(String(500), default="")
    lifecycle_detail: Mapped[str] = mapped_column(Text, default="")
    lifecycle_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lifecycle_by: Mapped[str] = mapped_column(String(100), default="")
    lifecycle_version: Mapped[int] = mapped_column(Integer, default=1)
    reference_revision: Mapped[int] = mapped_column(Integer, default=1)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ChannelMessage(Base):
    __tablename__ = "channel_messages"
    __table_args__ = (UniqueConstraint("tenant_id", "channel_target_id", "message_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    channel_target_id: Mapped[int] = mapped_column(ForeignKey("operation_targets.id"))
    message_id: Mapped[int] = mapped_column(Integer)
    message_url: Mapped[str] = mapped_column(String(300), default="")
    content_preview: Mapped[str] = mapped_column(Text, default="")
    comment_available: Mapped[bool] = mapped_column(Boolean, default=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ChannelMessageComment(Base):
    __tablename__ = "channel_message_comments"
    __table_args__ = (UniqueConstraint("tenant_id", "channel_target_id", "channel_message_id", "comment_message_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    channel_target_id: Mapped[int] = mapped_column(ForeignKey("operation_targets.id"))
    channel_message_id: Mapped[int] = mapped_column(ForeignKey("channel_messages.id"))
    comment_message_id: Mapped[int] = mapped_column(Integer)
    parent_comment_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author_peer_id: Mapped[str] = mapped_column(String(120), default="")
    author_username: Mapped[str] = mapped_column(String(120), default="")
    author_name: Mapped[str] = mapped_column(String(180), default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    content_preview: Mapped[str] = mapped_column(Text, default="")
    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class OperationTask(Base):
    __tablename__ = "operation_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    task_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[int | None] = mapped_column(ForeignKey("operation_targets.id"), nullable=True)
    target_reference_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_reference_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    channel_message_id: Mapped[int | None] = mapped_column(ForeignKey("channel_messages.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(180), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    reaction: Mapped[str] = mapped_column(String(32), default="")
    account_ids: Mapped[str] = mapped_column(Text, default="")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    actual_quantity: Mapped[int] = mapped_column(Integer, default=1)
    quantity_jitter_ratio: Mapped[int] = mapped_column(Integer, default=15)
    content_mode: Mapped[str] = mapped_column(String(20), default="literal")
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default=TaskStatus.QUEUED.value)
    failure_type: Mapped[str] = mapped_column(String(60), default="")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class OperationTaskAttempt(Base):
    __tablename__ = "operation_task_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    task_id: Mapped[int] = mapped_column(ForeignKey("operation_tasks.id"))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    action_type: Mapped[str] = mapped_column(String(40))
    content: Mapped[str] = mapped_column(Text, default="")
    reaction: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(30))
    failure_type: Mapped[str] = mapped_column(String(60), default="")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    remote_message_id: Mapped[str] = mapped_column(String(160), default="")
    idempotency_key: Mapped[str] = mapped_column(String(100), default="")
    planned_delay_seconds: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    gateway_call_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ManualOperationRecord(Base):
    __tablename__ = "manual_operation_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    target_id: Mapped[int | None] = mapped_column(ForeignKey("operation_targets.id"), nullable=True)
    operation_type: Mapped[str] = mapped_column(String(40), default="MESSAGE_SEND")
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30))
    failure_type: Mapped[str] = mapped_column(String(60), default="")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    remote_message_id: Mapped[str] = mapped_column(String(160), default="")
    gateway_call_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actor: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


__all__ = [
    "OperationTarget",
    "ChannelMessage",
    "ChannelMessageComment",
    "OperationTask",
    "OperationTaskAttempt",
    "ManualOperationRecord",
]
