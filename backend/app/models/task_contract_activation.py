from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class TaskContractActivationManifest(Base):
    __tablename__ = "task_contract_activation_manifests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "release_train", "route_epoch", name="uq_task_contract_route_epoch"),
        Index(
            "uq_task_contract_active_train",
            "tenant_id",
            "release_train",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(Integer)
    release_train: Mapped[str] = mapped_column(String(80))
    old_task_ids: Mapped[list] = mapped_column(JSON)
    new_task_ids: Mapped[list] = mapped_column(JSON)
    canary_task_id: Mapped[str] = mapped_column(String(36), default="")
    old_set_hash: Mapped[str] = mapped_column(String(64))
    new_config_set_hash: Mapped[str] = mapped_column(String(64))
    route_epoch: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(24), default="canary")
    version: Mapped[int] = mapped_column(Integer, default=1)
    approval_ref: Mapped[str] = mapped_column(String(160))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TaskContractRoute(Base):
    __tablename__ = "task_contract_routes"
    __table_args__ = (
        UniqueConstraint("manifest_id", "task_id", name="uq_task_contract_manifest_route"),
        Index("ix_task_contract_route_task", "task_id", "role"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    manifest_id: Mapped[str] = mapped_column(String(36))
    task_id: Mapped[str] = mapped_column(String(36))
    role: Mapped[str] = mapped_column(String(12))
    config_hash: Mapped[str] = mapped_column(String(64), default="")
    expected_lifecycle_epoch: Mapped[int] = mapped_column(Integer)


class TaskDeleteOperation(Base):
    __tablename__ = "task_delete_operations"
    __table_args__ = (
        UniqueConstraint("original_task_id", "expected_lifecycle_epoch", name="uq_task_delete_epoch"),
        Index("ix_task_delete_state", "state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    original_task_id: Mapped[str] = mapped_column(String(36))
    expected_lifecycle_epoch: Mapped[int] = mapped_column(Integer)
    expected_manifest_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="fencing")
    resume_stage: Mapped[str] = mapped_column(String(40), default="snapshot_runtime")
    stage_version: Mapped[int] = mapped_column(Integer, default=1)
    tombstone_set_hash: Mapped[str] = mapped_column(String(64), default="")
    delete_set_hash: Mapped[str] = mapped_column(String(64), default="")
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(160))
    approval_ref: Mapped[str] = mapped_column(String(160))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TaskDeleteOperationItem(Base):
    __tablename__ = "task_delete_operation_items"
    __table_args__ = (
        UniqueConstraint("operation_id", "entity_type", "entity_id", name="uq_task_delete_item"),
        Index("ix_task_delete_item_pending", "operation_id", "state", "entity_type", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("task_delete_operations.id", ondelete="CASCADE")
    )
    entity_type: Mapped[str] = mapped_column(String(48))
    entity_id: Mapped[str] = mapped_column(String(80))
    expected_state_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="pending")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RemoteMutationTombstone(Base):
    __tablename__ = "remote_mutation_tombstones"
    __table_args__ = (
        UniqueConstraint(
            "remote_mutation_key_hash",
            "gateway_request_hash",
            name="uq_remote_mutation_tombstone",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(Integer)
    original_task_id: Mapped[str] = mapped_column(String(36))
    mutation_kind: Mapped[str] = mapped_column(String(40))
    remote_mutation_key_hash: Mapped[str] = mapped_column(String(64))
    gateway_request_hash: Mapped[str] = mapped_column(String(64))
    remote_started: Mapped[bool] = mapped_column(Boolean)
    terminal_state: Mapped[str] = mapped_column(String(32))
    remote_fact_identity_hash: Mapped[str] = mapped_column(String(64), default="")
    reconcile_state: Mapped[str] = mapped_column(String(32), default="closed")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "RemoteMutationTombstone",
    "TaskContractActivationManifest",
    "TaskContractRoute",
    "TaskDeleteOperation",
    "TaskDeleteOperationItem",
]
