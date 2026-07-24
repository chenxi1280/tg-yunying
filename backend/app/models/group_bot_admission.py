from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


class GroupBotAdmissionPolicy(Base):
    """Target-scoped audited completion protocol for group-bot admission."""

    __tablename__ = "group_bot_admission_policies"
    __table_args__ = (
        Index("ix_group_bot_policy_group", "tenant_id", "group_id", "status"),
        Index("ix_group_bot_policy_bot", "tenant_id", "group_id", "trusted_bot_peer_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    group_id: Mapped[int] = mapped_column(Integer)
    trusted_bot_peer_id: Mapped[str] = mapped_column(String(80), default="")
    completion_policy: Mapped[str] = mapped_column(String(40))
    evidence_ref: Mapped[str] = mapped_column(String(255), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    policy_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    revoked_by: Mapped[str] = mapped_column(String(100), default="")
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class GroupBotAdmission(Base):
    """Per account+group group-bot admission fact, independent of can_send."""

    __tablename__ = "group_bot_admissions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "group_id",
            "account_id",
            name="uq_group_bot_admission_account",
        ),
        Index("ix_group_bot_admission_state", "tenant_id", "group_id", "state"),
        Index("ix_group_bot_admission_version", "tenant_id", "group_id", "account_id", "admission_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    group_id: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[int] = mapped_column(Integer)
    membership_action_id: Mapped[str] = mapped_column(String(36), default="")
    state: Mapped[str] = mapped_column(String(60), default="awaiting_group_bot_rule")
    completion_policy: Mapped[str] = mapped_column(String(40), default="")
    policy_version: Mapped[int] = mapped_column(Integer, default=0)
    admission_version: Mapped[int] = mapped_column(Integer, default=1)
    trusted_bot_peer_id: Mapped[str] = mapped_column(String(80), default="")
    join_start_cursor: Mapped[str] = mapped_column(String(80), default="")
    observed_end_cursor: Mapped[str] = mapped_column(String(80), default="")
    source_message_id: Mapped[str] = mapped_column(String(160), default="")
    confirmation_message_id: Mapped[str] = mapped_column(String(160), default="")
    transport_observation: Mapped[dict] = mapped_column(JSON, default=dict)
    post_send_visibility_state: Mapped[str] = mapped_column(String(40), default="")
    required_channel_refs: Mapped[list] = mapped_column(JSON, default=list)
    failure_code: Mapped[str] = mapped_column(String(80), default="")
    evidence_ref: Mapped[str] = mapped_column(String(255), default="")
    abandoned_reason: Mapped[str] = mapped_column(Text, default="")
    abandoned_by: Mapped[str] = mapped_column(String(100), default="")
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    join_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observation_closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class GroupBotRequiredChannelFollow(Base):
    """One exact required-channel follow per admission prompt reference."""

    __tablename__ = "group_bot_required_channel_follows"
    __table_args__ = (
        UniqueConstraint(
            "admission_id",
            "channel_ref",
            name="uq_group_bot_required_channel_follow",
        ),
        Index("ix_group_bot_channel_follow_status", "admission_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admission_id: Mapped[int] = mapped_column(ForeignKey("group_bot_admissions.id"))
    channel_ref: Mapped[str] = mapped_column(String(255))
    source_message_id: Mapped[str] = mapped_column(String(160), default="")
    action_id: Mapped[str] = mapped_column(String(36), default="")
    resolved_peer_id: Mapped[str] = mapped_column(String(80), default="")
    resolved_type: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    failure_code: Mapped[str] = mapped_column(String(80), default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class GroupBotAdmissionObservation(Base):
    """Auditable control observation batches after join."""

    __tablename__ = "group_bot_admission_observations"
    __table_args__ = (
        Index("ix_group_bot_observation_admission", "admission_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admission_id: Mapped[int] = mapped_column(ForeignKey("group_bot_admissions.id"))
    join_start_cursor: Mapped[str] = mapped_column(String(80), default="")
    observed_end_cursor: Mapped[str] = mapped_column(String(80), default="")
    listener_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    read_count: Mapped[int] = mapped_column(Integer, default=0)
    cursor_gap: Mapped[bool] = mapped_column(default=False)
    failure_code: Mapped[str] = mapped_column(String(80), default="")
    observation_version: Mapped[int] = mapped_column(Integer, default=1)
    result_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PendingVisibilityCredit(Base):
    """Hard-hourly credit hold while post-send visibility is pending."""

    __tablename__ = "pending_visibility_credits"
    __table_args__ = (
        UniqueConstraint("action_id", name="uq_pending_visibility_credit_action"),
        Index("ix_pending_visibility_credit_bucket", "bucket_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    bucket_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action_id: Mapped[str] = mapped_column(String(36), ForeignKey("actions.id"))
    execution_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    remote_message_id: Mapped[str] = mapped_column(String(160), default="")
    hold_reason: Mapped[str] = mapped_column(String(40), default="pending_visibility")
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "GroupBotAdmissionPolicy",
    "GroupBotAdmission",
    "GroupBotRequiredChannelFollow",
    "GroupBotAdmissionObservation",
    "PendingVisibilityCredit",
]
