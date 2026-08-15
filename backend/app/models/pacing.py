from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


class AccountPacingReservation(Base):
    __tablename__ = "account_pacing_reservations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "account_id",
            "pacing_slot_key",
            name="uq_account_pacing_reservation_slot",
        ),
        Index(
            "ix_account_pacing_reservation_timeline",
            "tenant_id",
            "account_id",
            "state",
            "effective_claim_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    pacing_slot_key: Mapped[str] = mapped_column(String(255))
    policy_version: Mapped[str] = mapped_column(String(48))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    release_not_before_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_claim_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    action_id: Mapped[str | None] = mapped_column(
        ForeignKey("actions.id", ondelete="SET NULL"),
        nullable=True,
    )
    state: Mapped[str] = mapped_column(String(24), default="reserved")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = ["AccountPacingReservation"]
