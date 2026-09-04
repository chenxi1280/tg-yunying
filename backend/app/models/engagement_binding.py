from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class TaskAccountGroupBindingSetRevision(Base):
    __tablename__ = "task_account_group_binding_set_revisions"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "task_lifecycle_epoch",
            "binding_set_revision",
            name="uq_task_account_group_binding_revision",
        ),
        Index(
            "uq_task_account_group_binding_active",
            "task_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
        Index("ix_task_account_group_binding_tenant", "tenant_id", "task_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    binding_set_revision: Mapped[int] = mapped_column(Integer, default=1)
    account_selection_mode: Mapped[str] = mapped_column(String(24), default="group")
    account_group_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    concurrency_limit_per_group: Mapped[int] = mapped_column(Integer, default=5)
    group_contracts: Mapped[list[dict]] = mapped_column(JSON, default=list)
    binding_set_hash: Mapped[str] = mapped_column(String(64))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state: Mapped[str] = mapped_column(String(24), default="active")
    supersedes_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_account_group_binding_set_revisions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AccountGroupMembershipSnapshotSet(Base):
    __tablename__ = "account_group_membership_snapshot_sets"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "task_lifecycle_epoch",
            "participation_unit",
            "snapshot_set_revision",
            name="uq_account_group_membership_snapshot_revision",
        ),
        Index(
            "ix_account_group_membership_snapshot_binding",
            "binding_set_revision_id",
            "participation_unit",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    binding_set_revision_id: Mapped[str] = mapped_column(
        ForeignKey("task_account_group_binding_set_revisions.id"), nullable=False
    )
    participation_unit: Mapped[str] = mapped_column(String(160))
    snapshot_set_revision: Mapped[int] = mapped_column(Integer, default=1)
    group_memberships: Mapped[list[dict]] = mapped_column(JSON, default=list)
    member_account_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    account_origin_groups: Mapped[dict] = mapped_column(JSON, default=dict)
    member_union_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="frozen")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "AccountGroupMembershipSnapshotSet",
    "TaskAccountGroupBindingSetRevision",
]
