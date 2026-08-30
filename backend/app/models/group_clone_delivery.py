from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class CloneDeliveryObligation(Base):
    __tablename__ = "clone_delivery_obligations"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "epoch",
            "source_event_id",
            "obligation_kind",
            "materialization_version",
            name="uq_clone_obligation_materialization",
        ),
        UniqueConstraint("task_id", "epoch", "sequencer_id", name="uq_clone_obligation_sequencer"),
        Index("ix_clone_obligation_state", "task_id", "state"),
        Index("ix_clone_obligation_stream_order", "task_id", "stream_order_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("clone_source_events.id", ondelete="CASCADE"))
    source_message_revision: Mapped[int] = mapped_column(Integer, default=1)
    obligation_kind: Mapped[str] = mapped_column(String(32))
    materialization_version: Mapped[int] = mapped_column(Integer, default=1)
    stream_order_no: Mapped[int] = mapped_column(BigInteger)
    sequencer_id: Mapped[int] = mapped_column(BigInteger)
    dependency_obligation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    dependency_source_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    binding_history_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    route_binding_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    execution_target_binding_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    album_manifest_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    topic_map_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sequencer_head_case_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    config_revision: Mapped[int] = mapped_column(Integer, default=1)
    sanitization_revision: Mapped[int] = mapped_column(Integer, default=1)
    media_policy_version: Mapped[int] = mapped_column(Integer, default=1)
    contract_version: Mapped[str] = mapped_column(String(32), default="v2_group_clone")
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unknown_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="observed")
    degradation_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class CloneMessagePart(Base):
    __tablename__ = "clone_message_parts"
    __table_args__ = (
        UniqueConstraint("task_id", "epoch", "obligation_id", "part_index", name="uq_clone_msg_part_index"),
        Index("ix_clone_msg_part_remote", "target_peer_type", "target_peer_id", "target_message_id"),
        Index("ix_clone_msg_part_source_msg", "task_id", "source_message_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    obligation_id: Mapped[str] = mapped_column(ForeignKey("clone_delivery_obligations.id", ondelete="CASCADE"))
    action_id: Mapped[str] = mapped_column(String(36))
    attempt_id: Mapped[str] = mapped_column(String(36))
    remote_fact_id: Mapped[str] = mapped_column(String(36))
    part_index: Mapped[int] = mapped_column(Integer, default=0)
    part_total: Mapped[int] = mapped_column(Integer, default=1)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    authorization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_authorizations.id", ondelete="CASCADE"))
    session_generation: Mapped[int] = mapped_column(Integer, default=1)
    execution_binding_hash: Mapped[str] = mapped_column(String(64))
    target_peer_type: Mapped[str] = mapped_column(String(32))
    target_peer_id: Mapped[str] = mapped_column(String(120))
    target_message_id: Mapped[int] = mapped_column(BigInteger)
    target_top_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gateway_mutation_identity_id: Mapped[str] = mapped_column(String(36))
    random_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gateway_request_identity: Mapped[str] = mapped_column(String(64))
    remote_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CloneManualReviewDecision(Base):
    __tablename__ = "clone_manual_review_decisions"
    __table_args__ = (
        UniqueConstraint("obligation_id", "review_revision", name="uq_clone_manual_review_rev"),
        UniqueConstraint("obligation_id", "client_request_id", name="uq_clone_manual_review_request"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    obligation_id: Mapped[str] = mapped_column(ForeignKey("clone_delivery_obligations.id", ondelete="CASCADE"))
    review_revision: Mapped[int] = mapped_column(Integer, default=1)
    client_request_id: Mapped[str] = mapped_column(String(100))
    decision: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_name: Mapped[str] = mapped_column(String(100), default="")
    reason: Mapped[str] = mapped_column(String(255), default="")
    before_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    after_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CloneSequencerHeadCase(Base):
    __tablename__ = "clone_sequencer_head_cases"
    __table_args__ = (
        UniqueConstraint("task_id", "epoch", "sequencer_id", "case_kind", name="uq_clone_seq_head_case_scope"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    sequencer_id: Mapped[int] = mapped_column(BigInteger)
    obligation_id: Mapped[str] = mapped_column(ForeignKey("clone_delivery_obligations.id", ondelete="CASCADE"))
    case_kind: Mapped[str] = mapped_column(String(32))
    failure_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    remote_mutation_started: Mapped[bool] = mapped_column(Boolean, default=False)
    authoritative_absence_evidence_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    policy_snapshot: Mapped[str] = mapped_column(String(32), default="fail_stop")
    state: Mapped[str] = mapped_column(String(32), default="waiting_decision")
    decision_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decision_actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class CloneCutoverExclusion(Base):
    __tablename__ = "clone_cutover_exclusions"
    __table_args__ = (
        UniqueConstraint("cutover_generation", "source_event_identity_hash", name="uq_clone_cutover_exclusion_identity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    cutover_generation: Mapped[int] = mapped_column(Integer, default=1)
    source_event_identity_hash: Mapped[str] = mapped_column(String(64))
    legacy_task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    clone_task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    mutation_owner_side: Mapped[str] = mapped_column(String(32))
    legacy_action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    clone_obligation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "CloneCutoverExclusion",
    "CloneDeliveryObligation",
    "CloneManualReviewDecision",
    "CloneMessagePart",
    "CloneSequencerHeadCase",
]
