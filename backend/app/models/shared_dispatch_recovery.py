from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class AiContentScopeTakeoverBatch(Base):
    __tablename__ = "ai_content_scope_takeover_batches"
    __table_args__ = (
        Index("ix_ai_scope_takeover_batch_status", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    dispatcher_scope: Mapped[str] = mapped_column(String(80))
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(160))
    classification_hash: Mapped[str] = mapped_column(String(64))
    classification_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="previewed")
    last_item_cursor: Mapped[str] = mapped_column(String(36), default="")
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    applied_count: Mapped[int] = mapped_column(Integer, default=0)
    noop_count: Mapped[int] = mapped_column(Integer, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0)
    quarantined_count: Mapped[int] = mapped_column(Integer, default=0)
    supersedes_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_content_scope_takeover_batches.id"), nullable=True,
    )
    release_version: Mapped[str] = mapped_column(String(80), default="")
    config_version: Mapped[str] = mapped_column(String(80), default="")
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now,
    )


class AiContentScopeTakeoverItem(Base):
    __tablename__ = "ai_content_scope_takeover_items"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "action_id", name="uq_ai_scope_takeover_item_action",
        ),
        Index("ix_ai_scope_takeover_item_pending", "batch_id", "status", "action_id"),
        Index("ix_ai_scope_takeover_item_action_id", "action_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("ai_content_scope_takeover_batches.id", ondelete="CASCADE"),
    )
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"))
    observed_action_state_hash: Mapped[str] = mapped_column(String(64))
    classification: Mapped[str] = mapped_column(String(40))
    classification_input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    outcome: Mapped[dict] = mapped_column(JSON, default=dict)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RemoteReconcileCase(Base):
    __tablename__ = "remote_reconcile_cases"
    __table_args__ = (
        UniqueConstraint(
            "action_id",
            "execution_attempt_id",
            name="uq_remote_reconcile_action_attempt",
        ),
        Index("ix_remote_reconcile_case_state", "state", "created_at"),
        Index(
            "ix_remote_reconcile_due",
            "next_probe_at",
            "id",
            postgresql_where=text("state = 'open'"),
            sqlite_where=text("state = 'open'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id"))
    execution_attempt_id: Mapped[str] = mapped_column(
        ForeignKey("execution_attempts.id"),
    )
    expected_action_state_hash: Mapped[str] = mapped_column(String(64))
    expected_attempt_state_hash: Mapped[str] = mapped_column(String(64))
    evidence_hash: Mapped[str] = mapped_column(String(64), default="")
    state: Mapped[str] = mapped_column(String(32), default="pending")
    actor: Mapped[str] = mapped_column(String(160), default="")
    evidence_source: Mapped[str] = mapped_column(String(120), default="")
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    remote_message_id: Mapped[str] = mapped_column(String(160), default="")
    remote_fact_id: Mapped[str] = mapped_column(String(160), default="")
    failure_code: Mapped[str] = mapped_column(String(120), default="")
    checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    next_probe_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    unknown_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now,
    )


class GatewayRequestEvidenceJournal(Base):
    __tablename__ = "gateway_request_evidence_journals"
    __table_args__ = (
        UniqueConstraint(
            "gateway_request_identity",
            name="uq_gateway_request_evidence_identity",
        ),
        Index("ix_gateway_request_evidence_state", "state", "observed_at"),
        Index("ix_gateway_request_evidence_action_id", "action_id"),
        Index("ix_gateway_request_evidence_attempt_id", "execution_attempt_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"))
    execution_attempt_id: Mapped[str] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="CASCADE"),
    )
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("tg_accounts.id"), nullable=True,
    )
    gateway_request_identity: Mapped[str] = mapped_column(String(160))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    target_fingerprint: Mapped[str] = mapped_column(String(64))
    result_fingerprint: Mapped[str] = mapped_column(String(64))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    remote_message_id: Mapped[str] = mapped_column(String(160), default="")
    remote_fact_id: Mapped[str] = mapped_column(String(160), default="")
    typed_remote_fact: Mapped[dict] = mapped_column(JSON, default=dict)
    failure_code: Mapped[str] = mapped_column(String(120), default="")
    remote_mutation_state: Mapped[str] = mapped_column(
        String(16), default="unknown",
    )
    state: Mapped[str] = mapped_column(String(20), default="recorded")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now,
    )


__all__ = [
    "AiContentScopeTakeoverBatch",
    "AiContentScopeTakeoverItem",
    "GatewayRequestEvidenceJournal",
    "RemoteReconcileCase",
]
