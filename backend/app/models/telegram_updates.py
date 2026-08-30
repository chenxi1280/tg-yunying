from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class TelegramAuthorizationUpdateState(Base):
    """
    账号授权级 Common Updates 权威状态（单 Collector 拥有 Common PTS/游标）。
    """
    __tablename__ = "telegram_authorization_update_states"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "authorization_id",
            "session_generation",
            name="uq_auth_update_state_scope",
        ),
        Index("ix_auth_update_state_lease", "owner_id", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    authorization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_authorizations.id", ondelete="CASCADE"))
    session_generation: Mapped[int] = mapped_column(Integer, default=1)
    common_pts: Mapped[int] = mapped_column(Integer, default=0)
    common_qts: Mapped[int] = mapped_column(Integer, default=0)
    common_seq: Mapped[int] = mapped_column(Integer, default=0)
    common_date: Mapped[int] = mapped_column(Integer, default=0)
    difference_cursor: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="initializing")  # initializing, catching_up, live, gap, blocked, stopped
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_fencing_epoch: Mapped[int] = mapped_column(Integer, default=1)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ingress_order_no: Mapped[int] = mapped_column(BigInteger, default=0)
    last_update_identity_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TelegramAuthorizationUpdateEvent(Base):
    """
    共享 Ingress 持久化事件信封（最小规范化摘要，不存整包原始 Updates）。
    """
    __tablename__ = "telegram_authorization_update_events"
    __table_args__ = (
        UniqueConstraint(
            "authorization_update_state_id",
            "ingress_order_no",
            name="uq_auth_update_event_order",
        ),
        UniqueConstraint(
            "authorization_update_state_id",
            "update_identity_hash",
            name="uq_auth_update_event_identity",
        ),
        Index("ix_auth_update_event_peer", "routing_peer_type", "routing_peer_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    authorization_update_state_id: Mapped[str] = mapped_column(
        ForeignKey("telegram_authorization_update_states.id", ondelete="CASCADE")
    )
    ingress_order_no: Mapped[int] = mapped_column(BigInteger)
    update_identity_hash: Mapped[str] = mapped_column(String(64))
    constructor_name: Mapped[str] = mapped_column(String(80))
    pts_evidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pts_count_evidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    routing_peer_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    routing_peer_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    payload_fingerprint: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TelegramAuthorizationUpdateSubscription(Base):
    """
    任务级 Update 订阅事实。
    """
    __tablename__ = "telegram_authorization_update_subscriptions"
    __table_args__ = (
        UniqueConstraint("task_id", "task_epoch", name="uq_auth_update_sub_task_epoch"),
        Index("ix_auth_update_sub_state", "authorization_update_state_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    authorization_update_state_id: Mapped[str] = mapped_column(
        ForeignKey("telegram_authorization_update_states.id", ondelete="CASCADE")
    )
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_epoch: Mapped[int] = mapped_column(Integer, default=1)
    source_peer_type: Mapped[str] = mapped_column(String(32))
    source_peer_id: Mapped[str] = mapped_column(String(120))
    start_ingress_order: Mapped[int] = mapped_column(BigInteger, default=0)
    state: Mapped[str] = mapped_column(String(32), default="initializing")  # initializing, active, paused, stopped
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TelegramAuthorizationUpdateDelivery(Base):
    """
    订阅交付事实（单 update 内稳定 item index 与规范化 payload）。
    """
    __tablename__ = "telegram_authorization_update_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "update_event_id",
            "subscription_id",
            "normalized_item_index",
            name="uq_auth_update_delivery_item",
        ),
        Index("ix_auth_update_delivery_task_state", "task_id", "delivery_state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    update_event_id: Mapped[str] = mapped_column(
        ForeignKey("telegram_authorization_update_events.id", ondelete="CASCADE")
    )
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("telegram_authorization_update_subscriptions.id", ondelete="CASCADE")
    )
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    normalized_item_index: Mapped[int] = mapped_column(Integer, default=0)
    normalized_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_fingerprint: Mapped[str] = mapped_column(String(64))
    delivery_state: Mapped[str] = mapped_column(String(32), default="pending")  # pending, delivered, consumed, skipped
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TelegramOutboundRandomIdMapping(Base):
    """
    Outbound random_id 到 Attempt 与 remote_message_id 的权威映射。
    """
    __tablename__ = "telegram_outbound_random_id_mappings"
    __table_args__ = (
        UniqueConstraint(
            "authorization_update_state_id",
            "random_id",
            name="uq_outbound_random_id_mapping",
        ),
        Index("ix_outbound_random_id_attempt", "action_id", "execution_attempt_id"),
        Index(
            "uq_outbound_random_id_gateway_request",
            "gateway_request_journal_id",
            unique=True,
            postgresql_where=text("gateway_request_journal_id IS NOT NULL"),
            sqlite_where=text("gateway_request_journal_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    authorization_update_state_id: Mapped[str] = mapped_column(
        ForeignKey("telegram_authorization_update_states.id", ondelete="CASCADE")
    )
    gateway_mutation_identity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    random_id: Mapped[int] = mapped_column(BigInteger)
    gateway_request_journal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    execution_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    target_peer_type: Mapped[str] = mapped_column(String(32))
    target_peer_id: Mapped[str] = mapped_column(String(120))
    remote_message_or_topic_id: Mapped[str] = mapped_column(String(120))
    update_identity_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "TelegramAuthorizationUpdateDelivery",
    "TelegramAuthorizationUpdateEvent",
    "TelegramAuthorizationUpdateState",
    "TelegramAuthorizationUpdateSubscription",
    "TelegramOutboundRandomIdMapping",
]
