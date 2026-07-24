from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


class ConversationSpeakerState(Base):
    """One lock/reservation row per real Telegram conversation."""

    __tablename__ = "conversation_speaker_states"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "surface",
            "conversation_key",
            name="uq_conversation_speaker_state_key",
        ),
        Index("ix_conversation_speaker_state_updated", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    surface: Mapped[str] = mapped_column(String(40), default="group_ai_chat")
    conversation_key: Mapped[str] = mapped_column(String(120))
    last_platform_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_platform_action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_platform_outcome: Mapped[str] = mapped_column(String(40), default="")
    last_platform_content_source: Mapped[str] = mapped_column(String(80), default="")
    last_human_cursor: Mapped[str] = mapped_column(String(80), default="")
    reserved_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reserved_action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ConversationSpeakerTurn(Base):
    """Append-only remote message order for speaker rotation."""

    __tablename__ = "conversation_speaker_turns"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "surface",
            "conversation_key",
            "remote_message_id",
            name="uq_conversation_speaker_turn_remote",
        ),
        Index(
            "ix_conversation_speaker_turn_lookup",
            "tenant_id",
            "surface",
            "conversation_key",
            "observed_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    surface: Mapped[str] = mapped_column(String(40), default="group_ai_chat")
    conversation_key: Mapped[str] = mapped_column(String(120))
    remote_message_id: Mapped[str] = mapped_column(String(160))
    remote_cursor: Mapped[str] = mapped_column(String(80), default="")
    sender_kind: Mapped[str] = mapped_column(String(40), default="platform")
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    outcome: Mapped[str] = mapped_column(String(40), default="")
    content_source: Mapped[str] = mapped_column(String(80), default="")
    content_preview: Mapped[str] = mapped_column(String(200), default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = ["ConversationSpeakerState", "ConversationSpeakerTurn"]
