from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _uuid() -> str:
    return str(uuid4())


class ChannelCommentGroundingSnapshot(Base):
    __tablename__ = "channel_comment_grounding_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "task_id", "channel_message_id", "comment_grounding_revision",
            name="uq_channel_comment_grounding_scope_revision",
        ),
        UniqueConstraint(
            "comment_plan_contract_id", "comment_grounding_revision",
            name="uq_channel_comment_grounding_plan_revision",
        ),
        UniqueConstraint(
            "comment_plan_contract_id", "source_revision_id", "grounding_policy_version",
            name="uq_channel_comment_grounding_source_policy",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    comment_plan_contract_id: Mapped[str] = mapped_column(
        ForeignKey("channel_comment_plan_contracts.id", ondelete="CASCADE"),
    )
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    channel_target_id: Mapped[int] = mapped_column(
        ForeignKey("operation_targets.id", ondelete="CASCADE"),
    )
    channel_message_id: Mapped[int] = mapped_column(
        ForeignKey("channel_messages.id", ondelete="CASCADE"),
    )
    source_remote_message_id: Mapped[int] = mapped_column(Integer)
    source_revision_id: Mapped[str] = mapped_column(
        ForeignKey("channel_message_source_revisions.id", ondelete="RESTRICT"),
    )
    comment_grounding_revision: Mapped[int] = mapped_column(Integer)
    supersedes_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("channel_comment_grounding_snapshots.id", ondelete="RESTRICT"),
        nullable=True,
    )
    grounding_contract_version: Mapped[str] = mapped_column(String(64))
    grounding_policy_version: Mapped[str] = mapped_column(String(64))
    extractor_version: Mapped[str] = mapped_column(String(64))
    content_route: Mapped[str] = mapped_column(String(48))
    content_route_revision: Mapped[int] = mapped_column(Integer)
    source_content_hash: Mapped[str] = mapped_column(String(64))
    source_state: Mapped[str] = mapped_column(String(32))
    teacher_state: Mapped[str] = mapped_column(String(32))
    teacher_candidates_json: Mapped[list] = mapped_column(JSON, default=list)
    aspect_evidence_json: Mapped[list] = mapped_column(JSON, default=list)
    evidence_blocks_json: Mapped[list] = mapped_column(JSON, default=list)
    semantic_capacity_policy_version: Mapped[str] = mapped_column(String(64))
    semantic_variant_units_json: Mapped[list] = mapped_column(JSON, default=list)
    groundable_capacity_count: Mapped[int] = mapped_column(Integer)
    extraction_audit_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelCommentGroundingEvaluation(Base):
    __tablename__ = "channel_comment_grounding_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "action_id", "generation_attempt_id", "candidate_content_hash",
            name="uq_channel_comment_grounding_evaluation_candidate",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"))
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"), nullable=True,
    )
    generation_attempt_id: Mapped[str] = mapped_column(String(80))
    candidate_content_hash: Mapped[str] = mapped_column(String(64))
    deterministic_evaluator_version: Mapped[str] = mapped_column(String(64))
    semantic_reviewer_request_id: Mapped[str] = mapped_column(String(80), default="")
    semantic_reviewer_model: Mapped[str] = mapped_column(String(120), default="")
    semantic_reviewer_schema_version: Mapped[str] = mapped_column(String(64), default="")
    semantic_reviewer_prompt_version: Mapped[str] = mapped_column(String(64), default="")
    semantic_reviewer_input_hash: Mapped[str] = mapped_column(String(64), default="")
    claim_results_json: Mapped[list] = mapped_column(JSON, default=list)
    primary_aspect_result: Mapped[str] = mapped_column(String(24))
    reply_relation_result: Mapped[str] = mapped_column(String(24))
    final_result: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = ["ChannelCommentGroundingEvaluation", "ChannelCommentGroundingSnapshot"]
