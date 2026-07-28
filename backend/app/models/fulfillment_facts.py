from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
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
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    channel_message_id: Mapped[int] = mapped_column(
        ForeignKey("channel_messages.id")
    )
    comment_plan_revision: Mapped[int] = mapped_column(Integer)
    target_ordinal: Mapped[int] = mapped_column(Integer)
    current_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("actions.id"),
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
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    channel_message_id: Mapped[int] = mapped_column(
        ForeignKey("channel_messages.id")
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    reaction_contract_version: Mapped[int] = mapped_column(Integer)
    current_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("actions.id"),
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
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    obligation_id: Mapped[str] = mapped_column(
        ForeignKey("reaction_fulfillment_obligations.id")
    )
    target_peer_id: Mapped[str] = mapped_column(String(120))
    channel_message_id: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
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
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_day_ledger_id: Mapped[str] = mapped_column(
        ForeignKey("task_day_ledgers.id", ondelete="CASCADE")
    )
    channel_message_id: Mapped[int] = mapped_column(
        ForeignKey("channel_messages.id")
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    current_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("actions.id"),
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
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    obligation_id: Mapped[str] = mapped_column(
        ForeignKey("view_fulfillment_obligations.id")
    )
    target_peer_id: Mapped[str] = mapped_column(String(120))
    channel_message_id: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
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
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_day_ledger_id: Mapped[str] = mapped_column(
        ForeignKey("task_day_ledgers.id", ondelete="CASCADE")
    )
    target_id: Mapped[int] = mapped_column(ForeignKey("operation_targets.id"))
    click_obligation_ordinal: Mapped[int] = mapped_column(Integer)
    source_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("actions.id"),
        nullable=True,
    )
    execution_attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_attempts.id"),
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
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
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
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
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
