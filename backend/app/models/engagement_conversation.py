from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class ConversationEvent(Base):
    __tablename__ = "conversation_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "surface", "canonical_peer_id", "remote_message_id",
            "event_revision", name="uq_conversation_event_remote_revision",
        ),
        UniqueConstraint(
            "source_context_message_id", name="uq_conversation_event_context_source"
        ),
        Index(
            "ix_conversation_event_peer_current",
            "tenant_id", "surface", "canonical_peer_id", "is_current", "sent_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    surface: Mapped[str] = mapped_column(String(40))
    canonical_peer_id: Mapped[str] = mapped_column(String(120))
    target_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("tg_groups.id", ondelete="CASCADE"), nullable=True
    )
    remote_message_id: Mapped[str] = mapped_column(String(160))
    event_revision: Mapped[int] = mapped_column(Integer, default=1)
    author_class: Mapped[str] = mapped_column(String(40))
    author_peer_id: Mapped[str] = mapped_column(String(120), default="")
    author_name: Mapped[str] = mapped_column(String(160), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64))
    modality: Mapped[str] = mapped_column(String(40), default="text")
    source_context_message_id: Mapped[int] = mapped_column(
        ForeignKey("group_context_messages.id", ondelete="CASCADE")
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ContextTurn(Base):
    __tablename__ = "context_turns"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "surface", "canonical_peer_id", "turn_family_key",
            name="uq_context_turn_family",
        ),
        Index(
            "ix_context_turn_due",
            "tenant_id", "surface", "canonical_peer_id", "state", "closed_at",
        ),
        Index("ix_context_turns_author", "tenant_id", "canonical_peer_id", "author_peer_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    surface: Mapped[str] = mapped_column(String(40))
    canonical_peer_id: Mapped[str] = mapped_column(String(120))
    target_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("tg_groups.id", ondelete="CASCADE"), nullable=True
    )
    turn_family_key: Mapped[str] = mapped_column(String(220))
    author_peer_id: Mapped[str] = mapped_column(String(120), default="")
    anchor_event_id: Mapped[str] = mapped_column(ForeignKey("conversation_events.id"))
    event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(24), default="assembling")
    first_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_count: Mapped[int] = mapped_column(Integer, default=1)
    topic_revision: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class InteractionOpportunity(Base):
    __tablename__ = "interaction_opportunities"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "task_lifecycle_epoch", "context_turn_id",
            name="uq_interaction_opportunity_task_turn",
        ),
        Index("ix_interaction_opportunity_task_state", "task_id", "state", "freshness_deadline_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer)
    context_turn_id: Mapped[str] = mapped_column(ForeignKey("context_turns.id", ondelete="CASCADE"))
    anchor_event_id: Mapped[str] = mapped_column(ForeignKey("conversation_events.id"))
    state: Mapped[str] = mapped_column(String(32), default="admitted")
    relation_kind: Mapped[str] = mapped_column(String(40), default="native_reply_external_human")
    natural_not_before_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    freshness_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ConversationTurnClaim(Base):
    __tablename__ = "conversation_turn_claims"
    __table_args__ = (
        UniqueConstraint("context_turn_id", name="uq_conversation_turn_claim_turn"),
        UniqueConstraint("interaction_opportunity_id", name="uq_conversation_turn_claim_opportunity"),
        Index("ix_conversation_turn_claim_task_state", "task_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    context_turn_id: Mapped[str] = mapped_column(ForeignKey("context_turns.id", ondelete="CASCADE"))
    interaction_opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("interaction_opportunities.id", ondelete="CASCADE")
    )
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id"), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="claimed")
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settlement_reason: Mapped[str] = mapped_column(String(80), default="")


class StageWakeOutbox(Base):
    __tablename__ = "stage_wake_outbox"
    __table_args__ = (
        UniqueConstraint(
            "aggregate_type", "aggregate_id", "aggregate_revision", "stage",
            name="uq_stage_wake_aggregate_revision",
        ),
        Index("ix_stage_wake_due", "state", "available_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    aggregate_type: Mapped[str] = mapped_column(String(40))
    aggregate_id: Mapped[str] = mapped_column(String(80))
    aggregate_revision: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(48))
    state: Mapped[str] = mapped_column(String(24), default="pending")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "ContextTurn",
    "ConversationEvent",
    "ConversationTurnClaim",
    "InteractionOpportunity",
    "StageWakeOutbox",
]
