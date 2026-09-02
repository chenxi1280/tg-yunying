from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class ChannelDiscussionGroupProbeEvent(Base):
    __tablename__ = "channel_discussion_group_probe_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "channel_target_id", "probe_request_id",
            name="uq_channel_discussion_group_probe_request",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    channel_target_id: Mapped[int] = mapped_column(ForeignKey("operation_targets.id", ondelete="CASCADE"))
    target_reference_revision: Mapped[int] = mapped_column(Integer)
    probe_request_id: Mapped[str] = mapped_column(String(80))
    probe_status: Mapped[str] = mapped_column(String(32))
    probe_stage: Mapped[str] = mapped_column(String(48))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    observed_linked_chat_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fresh_until_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelDiscussionGroupBinding(Base):
    __tablename__ = "channel_discussion_group_bindings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "channel_target_id", "binding_revision",
            name="uq_channel_discussion_group_binding_revision",
        ),
        Index(
            "uq_channel_discussion_group_binding_current",
            "tenant_id", "channel_target_id", unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current = true"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    channel_target_id: Mapped[int] = mapped_column(ForeignKey("operation_targets.id", ondelete="CASCADE"))
    target_reference_revision: Mapped[int] = mapped_column(Integer)
    binding_revision: Mapped[int] = mapped_column(Integer)
    channel_peer_id: Mapped[str] = mapped_column(String(160))
    discussion_target_id: Mapped[int | None] = mapped_column(ForeignKey("operation_targets.id"), nullable=True)
    discussion_peer_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    identity_hash: Mapped[str] = mapped_column(String(64))
    binding_status: Mapped[str] = mapped_column(String(24), default="active")
    probe_event_id: Mapped[str] = mapped_column(ForeignKey("channel_discussion_group_probe_events.id"))
    supersedes_binding_id: Mapped[str | None] = mapped_column(
        ForeignKey("channel_discussion_group_bindings.id", ondelete="RESTRICT"), nullable=True,
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fresh_until_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelDiscussionThreadProbeEvent(Base):
    __tablename__ = "channel_discussion_thread_probe_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_revision_id", "probe_request_id",
            name="uq_channel_discussion_thread_probe_request",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    source_revision_id: Mapped[str] = mapped_column(ForeignKey("channel_message_source_revisions.id"))
    group_binding_id: Mapped[str] = mapped_column(ForeignKey("channel_discussion_group_bindings.id"))
    probe_request_id: Mapped[str] = mapped_column(String(80))
    probe_status: Mapped[str] = mapped_column(String(32))
    probe_stage: Mapped[str] = mapped_column(String(48))
    observed_thread_root_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fresh_until_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelDiscussionThreadBinding(Base):
    __tablename__ = "channel_discussion_thread_bindings"
    __table_args__ = (
        UniqueConstraint(
            "source_revision_id", "group_binding_id", "thread_revision",
            name="uq_channel_discussion_thread_revision",
        ),
        Index(
            "uq_channel_discussion_thread_current",
            "source_revision_id", "group_binding_id", unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current = true"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    source_revision_id: Mapped[str] = mapped_column(ForeignKey("channel_message_source_revisions.id"))
    group_binding_id: Mapped[str] = mapped_column(ForeignKey("channel_discussion_group_bindings.id"))
    thread_revision: Mapped[int] = mapped_column(Integer)
    discussion_peer_id: Mapped[str] = mapped_column(String(160))
    thread_root_message_id: Mapped[int] = mapped_column(Integer)
    identity_hash: Mapped[str] = mapped_column(String(64))
    probe_event_id: Mapped[str] = mapped_column(ForeignKey("channel_discussion_thread_probe_events.id"))
    supersedes_thread_binding_id: Mapped[str | None] = mapped_column(
        ForeignKey("channel_discussion_thread_bindings.id", ondelete="RESTRICT"), nullable=True,
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DiscussionMembershipFact(Base):
    __tablename__ = "discussion_membership_facts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "account_id", "discussion_peer_id", "group_binding_id", "fact_revision",
            name="uq_discussion_membership_fact_revision",
        ),
        Index(
            "uq_discussion_membership_fact_current",
            "tenant_id", "account_id", "discussion_peer_id", "group_binding_id", unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current = true"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    discussion_peer_id: Mapped[str] = mapped_column(String(160))
    group_binding_id: Mapped[str] = mapped_column(ForeignKey("channel_discussion_group_bindings.id"))
    fact_revision: Mapped[int] = mapped_column(Integer)
    membership_status: Mapped[str] = mapped_column(String(32))
    can_send: Mapped[bool] = mapped_column(Boolean, default=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fresh_until_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    supersedes_fact_id: Mapped[str | None] = mapped_column(
        ForeignKey("discussion_membership_facts.id", ondelete="RESTRICT"), nullable=True,
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelCommentGroundingEnrollment(Base):
    __tablename__ = "channel_comment_grounding_enrollments"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "task_config_revision", "enrollment_revision",
            name="uq_channel_comment_grounding_enrollment_revision",
        ),
        Index(
            "uq_channel_comment_grounding_enrollment_active",
            "task_id", "task_config_revision", unique=True,
            sqlite_where=text("enrollment_state = 'active'"),
            postgresql_where=text("enrollment_state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_config_revision: Mapped[int] = mapped_column(Integer)
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer)
    enrollment_revision: Mapped[int] = mapped_column(Integer)
    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    contract_version: Mapped[str] = mapped_column(String(64))
    contracts_hash: Mapped[str] = mapped_column(String(64))
    group_binding_id: Mapped[str] = mapped_column(ForeignKey("channel_discussion_group_bindings.id"))
    group_binding_revision: Mapped[int] = mapped_column(Integer)
    group_binding_identity_hash: Mapped[str] = mapped_column(String(64))
    activation_hash: Mapped[str] = mapped_column(String(64), unique=True)
    operator_id: Mapped[str] = mapped_column(String(120))
    approval_reference: Mapped[str] = mapped_column(String(160))
    enrollment_state: Mapped[str] = mapped_column(String(24), default="active")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supersedes_enrollment_id: Mapped[str | None] = mapped_column(
        ForeignKey("channel_comment_grounding_enrollments.id", ondelete="RESTRICT"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelCommentListenerErrorEvent(Base):
    __tablename__ = "channel_comment_listener_error_events"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "subscription_id", "target_reference_revision", "listener_revision", "error_code",
            name="uq_channel_comment_listener_error_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    subscription_id: Mapped[str] = mapped_column(ForeignKey("task_source_subscriptions.id", ondelete="CASCADE"))
    target_reference_revision: Mapped[int] = mapped_column(Integer)
    listener_revision: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str] = mapped_column(String(80))
    error_state: Mapped[str] = mapped_column(String(24), default="active")
    detail: Mapped[str] = mapped_column(Text, default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelCommentRecoveryManifest(Base):
    __tablename__ = "channel_comment_recovery_manifests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    recovery_kind: Mapped[str] = mapped_column(String(64))
    expected_deployed_sha: Mapped[str] = mapped_column(String(64))
    expected_task_status: Mapped[str] = mapped_column(String(32))
    expected_task_config_revision: Mapped[int] = mapped_column(Integer)
    expected_task_lifecycle_epoch: Mapped[int] = mapped_column(Integer)
    expected_target_reference_revision: Mapped[int] = mapped_column(Integer)
    expected_binding_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expected_binding_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action_set_hash: Mapped[str] = mapped_column(String(64))
    exact_action_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    recovery_evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    state_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    preview_hash: Mapped[str] = mapped_column(String(64), unique=True)
    manifest_state: Mapped[str] = mapped_column(String(24), default="previewed")
    operator_id: Mapped[str] = mapped_column(String(120))
    approval_reference: Mapped[str] = mapped_column(String(160))
    previewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    readback_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "ChannelCommentGroundingEnrollment",
    "ChannelCommentListenerErrorEvent",
    "ChannelCommentRecoveryManifest",
    "ChannelDiscussionGroupBinding",
    "ChannelDiscussionGroupProbeEvent",
    "ChannelDiscussionThreadBinding",
    "ChannelDiscussionThreadProbeEvent",
    "DiscussionMembershipFact",
]
