from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class TaskTargetScopeClaim(Base):
    __tablename__ = "task_target_scope_claims"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "task_lifecycle_epoch",
            "adapter_type",
            "canonical_peer_id",
            name="uq_task_target_scope_epoch",
        ),
        Index(
            "uq_task_target_scope_active_writer",
            "tenant_id",
            "adapter_type",
            "canonical_peer_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
        Index("ix_task_target_scope_task_state", "task_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(40), nullable=False)
    canonical_peer_id: Mapped[str] = mapped_column(String(120), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    release_reason: Mapped[str] = mapped_column(String(80), default="", nullable=False)


__all__ = ["TaskTargetScopeClaim"]
