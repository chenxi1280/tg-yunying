from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class TaskPlannerWakeState(Base):
    __tablename__ = "task_planner_wake_states"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", name="uq_task_planner_wake_task"),
        Index("ix_task_planner_wake_due", "not_before_at", "wake_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    wake_revision: Mapped[int] = mapped_column(Integer, default=0)
    planned_revision: Mapped[int] = mapped_column(Integer, default=0)
    planning_revision: Mapped[int] = mapped_column(Integer, default=0)
    not_before_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(80), default="")
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TaskAdmissionProjection(Base):
    __tablename__ = "task_admission_projections"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "task_id",
            "lifecycle_epoch",
            name="uq_task_admission_projection_epoch",
        ),
        Index("ix_task_admission_projection_task", "tenant_id", "task_id", "lifecycle_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    scope_revision: Mapped[int] = mapped_column(Integer, default=0)
    item_revision: Mapped[int] = mapped_column(Integer, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    joined_count: Mapped[int] = mapped_column(Integer, default=0)
    pending_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    unknown_count: Mapped[int] = mapped_column(Integer, default=0)
    ready_count: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TaskRuntimeActiveBlocker(Base):
    __tablename__ = "task_runtime_active_blockers"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "task_id",
            "lifecycle_epoch",
            "blocker_domain",
            "scope_key_hash",
            name="uq_task_runtime_active_blocker_scope",
        ),
        Index(
            "ix_task_runtime_active_blocker_task",
            "tenant_id",
            "task_id",
            "lifecycle_epoch",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    blocker_domain: Mapped[str] = mapped_column(String(40))
    scope_key_hash: Mapped[str] = mapped_column(String(64))
    blocker_code: Mapped[str] = mapped_column(String(80))
    source_type: Mapped[str] = mapped_column(String(40), default="")
    source_id_hash: Mapped[str] = mapped_column(String(64), default="")
    source_revision: Mapped[int] = mapped_column(Integer, default=0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TaskSourceSubscription(Base):
    __tablename__ = "task_source_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "task_id",
            "lifecycle_epoch",
            "source_type",
            "source_peer_hash",
            name="uq_task_source_subscription_scope",
        ),
        Index("ix_task_source_subscription_listener", "listener_source_state_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    source_type: Mapped[str] = mapped_column(String(40))
    source_peer_hash: Mapped[str] = mapped_column(String(64))
    listener_source_state_id: Mapped[str | None] = mapped_column(
        ForeignKey("listener_source_state.id", ondelete="SET NULL"),
        nullable=True,
    )
    required_snapshot_revision: Mapped[int] = mapped_column(Integer, default=0)
    target_reference_revision: Mapped[int] = mapped_column(Integer, default=0)
    listener_revision: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ListenerChannelSnapshotItem(Base):
    __tablename__ = "listener_channel_snapshot_items"
    __table_args__ = (
        UniqueConstraint(
            "listener_source_state_id",
            "snapshot_revision",
            "channel_message_id",
            name="uq_listener_channel_snapshot_item",
        ),
        Index(
            "ix_listener_channel_snapshot_current",
            "listener_source_state_id",
            "snapshot_revision",
            "channel_message_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    listener_source_state_id: Mapped[str] = mapped_column(
        ForeignKey("listener_source_state.id", ondelete="CASCADE")
    )
    snapshot_revision: Mapped[int] = mapped_column(Integer)
    channel_message_id: Mapped[int] = mapped_column(
        ForeignKey("channel_messages.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourcePacingState(Base):
    __tablename__ = "source_pacing_states"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "pacing_domain",
            "source_key_hash",
            name="uq_source_pacing_state_source",
        ),
        Index("ix_source_pacing_state_timeline", "tenant_id", "pacing_domain", "source_key_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    pacing_domain: Mapped[str] = mapped_column(String(40))
    source_key_hash: Mapped[str] = mapped_column(String(64))
    next_call_not_before_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_call_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_source_gap_seconds: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class SourcePacingAdmission(Base):
    __tablename__ = "source_pacing_admissions"
    __table_args__ = (
        UniqueConstraint("admission_key", name="uq_source_pacing_admission_key"),
        Index(
            "ix_source_pacing_admission_due",
            "tenant_id",
            "state",
            "call_not_before_at",
        ),
        Index(
            "ix_source_pacing_admission_state_due",
            "source_pacing_state_id",
            "state",
            "call_not_before_at",
        ),
        Index("ix_source_pacing_admission_action", "action_id", "attempt_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    admission_key: Mapped[str] = mapped_column(String(160))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    source_pacing_state_id: Mapped[str] = mapped_column(
        ForeignKey("source_pacing_states.id", ondelete="CASCADE")
    )
    owner_type: Mapped[str] = mapped_column(String(50))
    owner_id: Mapped[str] = mapped_column(String(80))
    action_id: Mapped[str | None] = mapped_column(
        ForeignKey("actions.id", ondelete="SET NULL"),
        nullable=True,
    )
    attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    pacing_period_key: Mapped[str] = mapped_column(String(80))
    pacing_plan_hash: Mapped[str] = mapped_column(String(64))
    planned_release_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    call_not_before_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_gap_seconds: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="reserved")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class WorkerRuntimeResourceSample(Base):
    __tablename__ = "worker_runtime_resource_samples"
    __table_args__ = (
        Index("ix_worker_runtime_resource_role_time", "process_type", "captured_at"),
        Index("ix_worker_runtime_resource_worker_time", "worker_id_hash", "captured_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    worker_id_hash: Mapped[str] = mapped_column(String(64))
    process_type: Mapped[str] = mapped_column(String(40))
    release_sha: Mapped[str] = mapped_column(String(40), default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sample_interval_seconds: Mapped[int] = mapped_column(Integer, default=10)
    cgroup_version: Mapped[int] = mapped_column(Integer, default=0)
    rss_kib: Mapped[int] = mapped_column(Integer, default=0)
    pss_kib: Mapped[int] = mapped_column(Integer, default=0)
    private_dirty_kib: Mapped[int] = mapped_column(Integer, default=0)
    anonymous_kib: Mapped[int] = mapped_column(Integer, default=0)
    anon_huge_pages_kib: Mapped[int] = mapped_column(Integer, default=0)
    cgroup_current_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    cgroup_peak_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    cgroup_limit_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    cgroup_event_count: Mapped[int] = mapped_column(BigInteger, default=0)
    cpu_percent: Mapped[float] = mapped_column(Float, default=0)
    thread_count: Mapped[int] = mapped_column(Integer, default=0)
    telethon_client_count: Mapped[int] = mapped_column(Integer, default=0)
    drain_metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(24), default="fresh")


class WorkerRuntimeResourceRollup(Base):
    __tablename__ = "worker_runtime_resource_rollups"
    __table_args__ = (
        UniqueConstraint(
            "worker_id_hash",
            "process_type",
            "release_sha",
            "bucket_at",
            name="uq_worker_runtime_resource_rollup_bucket",
        ),
        Index("ix_worker_runtime_resource_rollup_time", "process_type", "bucket_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    worker_id_hash: Mapped[str] = mapped_column(String(64))
    process_type: Mapped[str] = mapped_column(String(40))
    release_sha: Mapped[str] = mapped_column(String(40), default="")
    bucket_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    pss_kib_p95: Mapped[int] = mapped_column(BigInteger, default=0)
    pss_kib_max: Mapped[int] = mapped_column(BigInteger, default=0)
    private_dirty_kib_p95: Mapped[int] = mapped_column(BigInteger, default=0)
    anonymous_kib_p95: Mapped[int] = mapped_column(BigInteger, default=0)
    cgroup_current_bytes_p95: Mapped[int] = mapped_column(BigInteger, default=0)
    cgroup_current_bytes_max: Mapped[int] = mapped_column(BigInteger, default=0)
    cgroup_event_count_max: Mapped[int] = mapped_column(BigInteger, default=0)
    cpu_percent_p95: Mapped[float] = mapped_column(Float, default=0)
    thread_count_max: Mapped[int] = mapped_column(Integer, default=0)
    telethon_client_count_max: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = [
    "ListenerChannelSnapshotItem",
    "SourcePacingAdmission",
    "SourcePacingState",
    "TaskAdmissionProjection",
    "TaskPlannerWakeState",
    "TaskRuntimeActiveBlocker",
    "TaskSourceSubscription",
    "WorkerRuntimeResourceSample",
    "WorkerRuntimeResourceRollup",
]
