from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now
from .task_center import new_uuid


class TargetRuntimeSummary(Base):
    __tablename__ = "target_runtime_summary"
    __table_args__ = (
        UniqueConstraint("tenant_id", "target_id", name="uq_target_runtime_summary_target"),
        Index("ix_target_runtime_summary_status", "tenant_id", "target_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    target_id: Mapped[int] = mapped_column(ForeignKey("operation_targets.id"))
    status: Mapped[str] = mapped_column(String(40), default="healthy")
    open_issue_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_action_count: Mapped[int] = mapped_column(Integer, default=0)
    affected_task_count: Mapped[int] = mapped_column(Integer, default=0)
    latest_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TaskRuntimeSummary(Base):
    __tablename__ = "task_runtime_summary"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", name="uq_task_runtime_summary_task"),
        Index("ix_task_runtime_summary_task", "tenant_id", "task_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    task_status: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[int | None] = mapped_column(ForeignKey("operation_targets.id"), nullable=True)
    planned_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    pending_count: Mapped[int] = mapped_column(Integer, default=0)
    oldest_pending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latest_failure_type: Mapped[str] = mapped_column(String(80), default="")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AccountRuntimeSummary(Base):
    __tablename__ = "account_runtime_summary"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_id", name="uq_account_runtime_summary_account"),
        Index("ix_account_runtime_summary_account", "tenant_id", "account_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    send_available: Mapped[bool] = mapped_column(Boolean, default=False)
    listen_available: Mapped[bool] = mapped_column(Boolean, default=False)
    join_available: Mapped[bool] = mapped_column(Boolean, default=False)
    comment_available: Mapped[bool] = mapped_column(Boolean, default=False)
    profile_available: Mapped[bool] = mapped_column(Boolean, default=False)
    code_read_available: Mapped[bool] = mapped_column(Boolean, default=False)
    remaining_capacity: Mapped[int] = mapped_column(Integer, default=0)
    unavailable_reason: Mapped[str] = mapped_column(Text, default="")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_trend: Mapped[dict] = mapped_column(JSON, default=dict)
    health_score: Mapped[float] = mapped_column(Float, default=100)
    risk_level: Mapped[str] = mapped_column(String(20), default="A")
    score_reasons: Mapped[list] = mapped_column(JSON, default=list)
    non_score_reasons: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class OperationIssue(Base):
    __tablename__ = "operation_issue"
    __table_args__ = (
        Index("ix_operation_issue_target_status", "tenant_id", "target_id", "status"),
        Index("ix_operation_issue_type_status", "tenant_id", "issue_type", "failure_type", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("operation_targets.id"), nullable=True)
    issue_type: Mapped[str] = mapped_column(String(80), default="")
    severity: Mapped[str] = mapped_column(String(40), default="warning")
    source_task_id: Mapped[str] = mapped_column(String(36), default="")
    representative_action_id: Mapped[str] = mapped_column(String(36), default="")
    affected_task_count: Mapped[int] = mapped_column(Integer, default=0)
    affected_account_count: Mapped[int] = mapped_column(Integer, default=0)
    affected_account_ids: Mapped[list] = mapped_column(JSON, default=list)
    failure_type: Mapped[str] = mapped_column(String(80), default="")
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    suggested_action: Mapped[str] = mapped_column(Text, default="")
    handling_mode: Mapped[str] = mapped_column(String(30), default="modal")
    return_to: Mapped[dict] = mapped_column(JSON, default=dict)
    claimed_by: Mapped[str] = mapped_column(String(100), default="")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="open")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class OperationIssueSource(Base):
    __tablename__ = "operation_issue_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "issue_id", "source_type", "source_id", name="uq_operation_issue_sources_source"),
        Index("ix_operation_issue_sources_issue", "tenant_id", "issue_id", "latest_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    issue_id: Mapped[str] = mapped_column(ForeignKey("operation_issue.id"))
    source_type: Mapped[str] = mapped_column(String(40), default="")
    source_id: Mapped[str] = mapped_column(String(80), default="")
    failure_type: Mapped[str] = mapped_column(String(80), default="")
    latest_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)


class OperationIssueAccount(Base):
    __tablename__ = "operation_issue_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "issue_id", "account_id", "impact_type", name="uq_operation_issue_accounts_account"),
        Index("ix_operation_issue_accounts_issue", "tenant_id", "issue_id", "latest_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    issue_id: Mapped[str] = mapped_column(ForeignKey("operation_issue.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    impact_type: Mapped[str] = mapped_column(String(80), default="execution_failure")
    latest_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)


__all__ = [
    "AccountRuntimeSummary",
    "OperationIssue",
    "OperationIssueAccount",
    "OperationIssueSource",
    "TargetRuntimeSummary",
    "TaskRuntimeSummary",
]
