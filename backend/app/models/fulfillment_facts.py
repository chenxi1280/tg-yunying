from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class CommentFulfillmentObligation(Base):
    __tablename__ = "comment_fulfillment_obligations"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "channel_message_id",
            "comment_plan_revision",
            "target_ordinal",
            name="uq_comment_fulfillment_natural_key",
        ),
        UniqueConstraint(
            "telegram_discussion_peer_id",
            "remote_comment_id",
            name="uq_comment_fulfillment_remote_fact",
        ),
        Index("ix_comment_fulfillment_status", "task_id", "status"),
        Index("ix_comment_fulfillment_current_action", "current_action_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    channel_message_id: Mapped[int] = mapped_column(
        ForeignKey("channel_messages.id", ondelete="CASCADE")
    )
    comment_plan_revision: Mapped[int] = mapped_column(Integer)
    target_ordinal: Mapped[int] = mapped_column(Integer)
    content_mix_contract_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_mix_contracts.id"),
        nullable=True,
    )
    relation_kind: Mapped[str] = mapped_column(String(16), default="direct")
    reply_to_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reply_target_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    current_action_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "actions.id",
            ondelete="CASCADE",
            name="fk_comment_fulfillment_current_action",
        ),
        nullable=True,
    )
    action_attempt_no: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="open")
    telegram_discussion_peer_id: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
    )
    remote_comment_id: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
    )
    remote_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReactionFulfillmentObligation(Base):
    __tablename__ = "reaction_fulfillment_obligations"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "channel_message_id",
            "account_id",
            "reaction_contract_version",
            name="uq_reaction_fulfillment_natural_key",
        ),
        Index("ix_reaction_fulfillment_status", "task_id", "status"),
        Index("ix_reaction_fulfillment_current_action", "current_action_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    channel_message_id: Mapped[int] = mapped_column(
        ForeignKey("channel_messages.id", ondelete="CASCADE")
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    reaction_contract_version: Mapped[int] = mapped_column(Integer)
    current_action_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "actions.id",
            ondelete="CASCADE",
            name="fk_reaction_fulfillment_current_action",
        ),
        nullable=True,
    )
    action_attempt_no: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReactionRemoteFact(Base):
    __tablename__ = "reaction_remote_facts"
    __table_args__ = (
        UniqueConstraint(
            "target_peer_id",
            "channel_message_id",
            "account_id",
            "reaction_state_revision",
            name="uq_reaction_remote_fact_revision",
        ),
        UniqueConstraint(
            "reaction_evidence_hash",
            name="uq_reaction_remote_fact_evidence",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    obligation_id: Mapped[str] = mapped_column(
        ForeignKey("reaction_fulfillment_obligations.id", ondelete="CASCADE")
    )
    target_peer_id: Mapped[str] = mapped_column(String(120))
    channel_message_id: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    reaction_state_revision: Mapped[str] = mapped_column(String(120))
    reaction_evidence_hash: Mapped[str] = mapped_column(String(64))
    remote_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ViewFulfillmentObligation(Base):
    __tablename__ = "view_fulfillment_obligations"
    __table_args__ = (
        UniqueConstraint(
            "task_day_ledger_id",
            "channel_message_id",
            "account_id",
            name="uq_view_fulfillment_natural_key",
        ),
        Index("ix_view_fulfillment_status", "task_day_ledger_id", "status"),
        Index("ix_view_fulfillment_current_action", "current_action_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_day_ledger_id: Mapped[str] = mapped_column(
        ForeignKey("task_day_ledgers.id", ondelete="CASCADE")
    )
    channel_message_id: Mapped[int] = mapped_column(
        ForeignKey("channel_messages.id", ondelete="CASCADE")
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    current_action_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "actions.id",
            ondelete="CASCADE",
            name="fk_view_fulfillment_current_action",
        ),
        nullable=True,
    )
    action_attempt_no: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ViewRemoteFact(Base):
    __tablename__ = "view_remote_facts"
    __table_args__ = (
        UniqueConstraint(
            "target_peer_id",
            "channel_message_id",
            "account_id",
            name="uq_view_remote_fact_lifetime_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    obligation_id: Mapped[str] = mapped_column(
        ForeignKey("view_fulfillment_obligations.id", ondelete="CASCADE")
    )
    target_peer_id: Mapped[str] = mapped_column(String(120))
    channel_message_id: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    remote_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SearchClickFulfillmentObligation(Base):
    __tablename__ = "search_click_fulfillment_obligations"
    __table_args__ = (
        UniqueConstraint(
            "task_day_ledger_id",
            "target_id",
            "click_obligation_ordinal",
            name="uq_search_click_fulfillment_natural_key",
        ),
        UniqueConstraint(
            "click_evidence_hash",
            name="uq_search_click_fulfillment_evidence",
        ),
        Index(
            "ix_search_click_fulfillment_status",
            "task_day_ledger_id",
            "status",
        ),
        Index("ix_search_click_fulfillment_source_action", "source_action_id"),
        Index("ix_search_click_fulfillment_attempt", "execution_attempt_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_day_ledger_id: Mapped[str] = mapped_column(
        ForeignKey("task_day_ledgers.id", ondelete="CASCADE")
    )
    target_id: Mapped[int] = mapped_column(ForeignKey("operation_targets.id", ondelete="CASCADE"))
    click_obligation_ordinal: Mapped[int] = mapped_column(Integer)
    source_action_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "actions.id",
            ondelete="CASCADE",
            name="fk_search_click_fulfillment_source_action",
        ),
        nullable=True,
    )
    execution_attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="CASCADE"),
        nullable=True,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="open")
    target_click_observed: Mapped[bool] = mapped_column(Boolean, default=False)
    click_evidence_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    remote_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ConsistencyQuarantine(Base):
    __tablename__ = "consistency_quarantines"
    __table_args__ = (
        UniqueConstraint(
            "scope_type",
            "scope_id",
            "issue_fingerprint",
            name="uq_consistency_quarantine_issue",
        ),
        Index("ix_consistency_quarantine_active", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    scope_type: Mapped[str] = mapped_column(String(40))
    scope_id: Mapped[str] = mapped_column(String(120))
    reason_code: Mapped[str] = mapped_column(String(80))
    issue_fingerprint: Mapped[str] = mapped_column(String(64))
    observed_state: Mapped[str] = mapped_column(Text, default="")
    trigger: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TaskDayLedgerLifecycleEvent(Base):
    __tablename__ = "task_day_ledger_lifecycle_events"
    __table_args__ = (
        Index(
            "ix_task_day_ledger_lifecycle_events_ledger",
            "task_day_ledger_id",
            "occurred_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_day_ledger_id: Mapped[str] = mapped_column(
        ForeignKey("task_day_ledgers.id", ondelete="CASCADE")
    )
    event_type: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    task_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "CommentFulfillmentObligation",
    "ConsistencyQuarantine",
    "ReactionFulfillmentObligation",
    "ReactionRemoteFact",
    "SearchClickFulfillmentObligation",
    "TaskDayLedgerLifecycleEvent",
    "ViewFulfillmentObligation",
    "ViewRemoteFact",
]
