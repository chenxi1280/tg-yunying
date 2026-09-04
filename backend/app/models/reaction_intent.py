from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class ReactionIntentPolicyRevision(Base):
    __tablename__ = "reaction_intent_policy_revisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "revision", name="uq_reaction_intent_policy_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(32), default="active")
    intent_mappings: Mapped[dict] = mapped_column(JSON, default=dict)
    safe_intents: Mapped[list] = mapped_column(JSON, default=list)
    negative_keywords: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceReactionIntentDecision(Base):
    __tablename__ = "source_reaction_intent_decisions"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "account_id", "source_revision", "policy_revision_id",
            name="uq_source_reaction_intent_decision",
        ),
        Index("ix_reaction_intent_task_source", "task_id", "source_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    source_revision: Mapped[str] = mapped_column(String(64))
    policy_revision_id: Mapped[str] = mapped_column(String(36))
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    detected_intents: Mapped[list] = mapped_column(JSON, default=list)
    has_negative_keywords: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_reactions: Mapped[list] = mapped_column(JSON, default=list)
    configured_reactions: Mapped[list] = mapped_column(JSON, default=list)
    candidate_reactions: Mapped[list] = mapped_column(JSON, default=list)
    chosen_reaction: Mapped[str] = mapped_column(String(32), default="")
    decision: Mapped[str] = mapped_column(String(40), default="confirmed")
    reason: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "ReactionIntentPolicyRevision",
    "SourceReactionIntentDecision",
]
