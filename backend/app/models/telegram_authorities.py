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


class TelegramGroupMutationAuthority(Base):
    """
    平台级目标群写入权威（shared / exclusive_clone / handoff）。
    """
    __tablename__ = "telegram_group_mutation_authorities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "target_peer_type",
            "target_peer_id",
            name="uq_group_mutation_auth_target",
        ),
        Index("ix_group_mutation_auth_mode", "mode", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    target_peer_type: Mapped[str] = mapped_column(String(32))
    target_peer_id: Mapped[str] = mapped_column(String(120))
    mode: Mapped[str] = mapped_column(String(32), default="shared")  # shared, exclusive_clone, handoff, vacant
    cutover_generation: Mapped[int] = mapped_column(Integer, default=1)
    gateway_admission_side: Mapped[str] = mapped_column(String(32), default="all")  # all, new, old, none
    state: Mapped[str] = mapped_column(String(32), default="active")  # active, paused, closed
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TelegramGroupMutationAuthorityHolder(Base):
    """
    目标群写权限持有者登记（writer_kind, writer_id, route_hash）。
    """
    __tablename__ = "telegram_group_mutation_authority_holders"
    __table_args__ = (
        UniqueConstraint(
            "authority_id",
            "writer_kind",
            "writer_id",
            "route_hash",
            name="uq_group_mutation_auth_holder",
        ),
        Index("ix_group_mutation_auth_holder_writer", "writer_kind", "writer_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    authority_id: Mapped[str] = mapped_column(
        ForeignKey("telegram_group_mutation_authorities.id", ondelete="CASCADE")
    )
    writer_kind: Mapped[str] = mapped_column(String(32))  # group_clone, group_ai_chat, group_relay, message_task, campaign
    writer_id: Mapped[str] = mapped_column(String(64))  # task_id or campaign_id
    route_hash: Mapped[str] = mapped_column(String(64))
    holder_role: Mapped[str] = mapped_column(String(32), default="primary")  # primary, old_handoff, new_handoff, shared_member
    state: Mapped[str] = mapped_column(String(32), default="active")  # active, releasing, released
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TelegramAuthorizationTransportState(Base):
    """
    平台级账号全局 Transport State（FloodWait 冷却与 SlowMode）。
    """
    __tablename__ = "telegram_authorization_transport_states"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "authorization_id",
            "session_generation",
            "scope_type",
            "target_peer_key",
            name="uq_auth_transport_scope",
        ),
        Index("ix_auth_transport_blocked", "blocked_until"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    authorization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_authorizations.id", ondelete="CASCADE"))
    session_generation: Mapped[int] = mapped_column(Integer, default=1)
    scope_type: Mapped[str] = mapped_column(String(32), default="global")  # global (FloodWait), target_slowmode
    target_peer_key: Mapped[str] = mapped_column(String(120), default="*")
    blocked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(80), default="flood_wait")
    source_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    version: Mapped[int] = mapped_column(Integer, default=1)


__all__ = [
    "TelegramAuthorizationTransportState",
    "TelegramGroupMutationAuthority",
    "TelegramGroupMutationAuthorityHolder",
]
