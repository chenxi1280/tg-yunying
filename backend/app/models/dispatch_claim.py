from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class DispatchClaimScope(Base):
    __tablename__ = "dispatch_claim_scopes"
    __table_args__ = (Index("ix_dispatch_claim_scope_updated", "updated_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    dispatcher_scope: Mapped[str] = mapped_column(String(80), unique=True)
    claim_capacity: Mapped[int] = mapped_column(Integer, default=0)
    active_claim_count: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class DispatchClaimWindow(Base):
    __tablename__ = "dispatch_claim_windows"
    __table_args__ = (
        UniqueConstraint("dispatcher_scope", "bucket_start", "bucket_end", name="uq_dispatch_claim_window_scope_bucket"),
        Index("ix_dispatch_claim_window_scope_start", "dispatcher_scope", "bucket_start"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    dispatcher_scope: Mapped[str] = mapped_column(String(80))
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bucket_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claim_capacity: Mapped[int] = mapped_column(Integer, default=0)
    active_claim_count: Mapped[int] = mapped_column(Integer, default=0)
    unclaimed_allocated_count: Mapped[int] = mapped_column(Integer, default=0)
    allocation_epoch: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class DispatchClaimShardAllocation(Base):
    __tablename__ = "dispatch_claim_shard_allocations"
    __table_args__ = (
        UniqueConstraint("dispatch_claim_window_id", "account_shard_total", "account_shard_index", name="uq_dispatch_claim_shard_window"),
        Index("ix_dispatch_claim_shard_window", "dispatch_claim_window_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    dispatch_claim_window_id: Mapped[str] = mapped_column(ForeignKey("dispatch_claim_windows.id", ondelete="CASCADE"))
    account_shard_total: Mapped[int] = mapped_column(Integer, default=1)
    account_shard_index: Mapped[int] = mapped_column(Integer, default=0)
    required_claims: Mapped[int] = mapped_column(Integer, default=0)
    active_claim_count: Mapped[int] = mapped_column(Integer, default=0)
    unclaimed_allocated_count: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(120), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class DispatchClaimReservation(Base):
    __tablename__ = "dispatch_claim_reservations"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_claim_shard_allocation_id", "tenant_id", "task_id", "claim_class",
            name="uq_dispatch_claim_reservation_scope",
        ),
        Index("ix_dispatch_claim_reservation_allocation", "dispatch_claim_shard_allocation_id", "claim_class"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    dispatch_claim_shard_allocation_id: Mapped[str] = mapped_column(ForeignKey("dispatch_claim_shard_allocations.id", ondelete="CASCADE"))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    claim_class: Mapped[str] = mapped_column(String(40))
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    required_claims: Mapped[int] = mapped_column(Integer, default=0)
    reserved_claims: Mapped[int] = mapped_column(Integer, default=0)
    claimed_count: Mapped[int] = mapped_column(Integer, default=0)
    urgency_score: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(120), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = ["DispatchClaimReservation", "DispatchClaimScope", "DispatchClaimShardAllocation", "DispatchClaimWindow"]
